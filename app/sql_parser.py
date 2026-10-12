"""Streaming MySQL dump reader and INSERT row parser.

The reader never loads the whole dump: it keeps a bounded byte buffer and uses a
single possessive regular expression to find the end of each statement. String
literals, quoted identifiers and comments are consumed as atomic units, so
semicolons, commas or newlines inside them never terminate a statement.

If a construct is cut by the end of the buffer, the expression cannot reach a
terminating ``;`` (each partial construct can only fail, never be reinterpreted
as something that runs past the cut), so the reader simply reads more data and
retries.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Iterator

# --------------------------------------------------------------------------- #
# Statement splitting
# --------------------------------------------------------------------------- #

_STATEMENT_RE = re.compile(
    rb"""
    (?:
        [^;'"`/\#\-]++                         # ordinary SQL text
      | '(?:[^'\\]++|\\.|'')*+'                 # single-quoted string
      | "(?:[^"\\]++|\\.|"")*+"                 # double-quoted string
      | `(?:[^`]++|``)*+`                       # quoted identifier
      | /\*.*?\*/                               # block / version comment
      | /(?!\*)                                 # lone slash (division)
      | --(?=[ \t\r\n])[^\n]*+                  # '-- ' line comment
      | --(?=[^ \t\r\n])                        # '--' that is not a comment
      | -(?!-)                                  # lone minus
      | \#[^\n]*+                               # '#' line comment
    )*+
    ;
    """,
    re.VERBOSE | re.DOTALL,
)

# Leading whitespace and ordinary (non-version) comments before a statement.
_LEADING_RE = re.compile(
    rb"""(?:\s++|--(?=\s|$)[^\n]*+(?:\n|$)|\#[^\n]*+(?:\n|$)|/\*(?!!).*?\*/)*+""",
    re.DOTALL,
)

_INSERT_HEAD_RE = re.compile(
    rb"""
    (INSERT|REPLACE)\s+
    (?:(?:LOW_PRIORITY|DELAYED|HIGH_PRIORITY)\s+)?
    (IGNORE\s+)?
    (?:INTO\s+)?
    (?:(`(?:[^`]|``)+`|[A-Za-z0-9_$]+)\s*\.\s*)?          # optional schema
    (`(?:[^`]|``)+`|[A-Za-z0-9_$]+)                       # table
    \s*
    (\(\s*(?:`(?:[^`]|``)+`|[A-Za-z0-9_$]+)
        (?:\s*,\s*(?:`(?:[^`]|``)+`|[A-Za-z0-9_$]+))*\s*\))?   # column list
    \s*
    (VALUES?|SET|SELECT|\()
    """,
    re.VERBOSE | re.IGNORECASE | re.DOTALL,
)

_IDENT_RE = re.compile(rb"`((?:[^`]|``)+)`|([A-Za-z0-9_$]+)")
_CREATE_TABLE_RE = re.compile(
    rb"CREATE\s+(?:TEMPORARY\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    rb"(?:(?:`(?:[^`]|``)+`|[A-Za-z0-9_$]+)\s*\.\s*)?(`(?:[^`]|``)+`|[A-Za-z0-9_$]+)",
    re.IGNORECASE,
)
_STRING_LITERAL_RE = re.compile(rb"""'(?:[^'\\]++|\\.|'')*+'""", re.DOTALL)


class SqlParseError(Exception):
    """Raised for malformed or unsupported SQL, with a source location."""

    def __init__(self, message: str, *, line: int | None = None, offset: int | None = None,
                 context: str | None = None):
        self.message = message
        self.line = line
        self.offset = offset
        self.context = context
        loc = []
        if line is not None:
            loc.append(f"line {line}")
        if offset is not None:
            loc.append(f"byte offset {offset}")
        text = message + (f" ({', '.join(loc)})" if loc else "")
        if context:
            text += f" near: {context!r}"
        super().__init__(text)


@dataclass(frozen=True)
class Statement:
    """A complete SQL statement located in the source file."""

    raw: bytes            # statement bytes including leading whitespace/comments and the ';'
    offset: int           # byte offset of `raw` in the file (seekable resume point)
    end_offset: int       # byte offset just past the terminating ';'
    line: int             # 1-based line number of the first significant character
    body_start: int       # index into `raw` of the first significant character

    @property
    def body(self) -> bytes:
        return self.raw[self.body_start:]

    @property
    def offset_line(self) -> int:
        """Line number at `offset` (use as `start_line` when seeking to `offset`)."""
        return self.line - self.raw.count(b"\n", 0, self.body_start)

    def head(self, n: int = 512) -> bytes:
        return self.raw[self.body_start:self.body_start + n]


def _decode_ident(token: bytes) -> str:
    m = _IDENT_RE.fullmatch(token.strip())
    if not m:
        return token.decode("utf-8", "replace")
    if m.group(1) is not None:
        return m.group(1).replace(b"``", b"`").decode("utf-8", "replace")
    return m.group(2).decode("utf-8", "replace")


def classify(stmt: Statement) -> tuple[str, str | None]:
    """Return (kind, table_name) for a statement.

    kind is one of: insert, replace, create_table, drop_table, lock, unlock,
    set, conditional_comment, create_database, drop_database, use, other.
    """
    head = stmt.head(8192)
    upper = head[:40].upper()
    if upper.startswith(b"/*!"):
        return "conditional_comment", None
    if upper.startswith((b"INSERT", b"REPLACE")):
        m = _INSERT_HEAD_RE.match(head)
        if m:
            return m.group(1).decode().lower(), _decode_ident(m.group(4))
        return "insert_unparsed", None
    if upper.startswith(b"CREATE"):
        if re.match(rb"CREATE\s+(DATABASE|SCHEMA)\b", upper, re.I):
            return "create_database", None
        m = _CREATE_TABLE_RE.match(head)
        if m:
            return "create_table", _decode_ident(m.group(1))
        return "other", None
    if upper.startswith(b"DROP"):
        if re.match(rb"DROP\s+(DATABASE|SCHEMA)\b", upper, re.I):
            return "drop_database", None
        m = re.match(rb"DROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?(`(?:[^`]|``)+`|[A-Za-z0-9_$]+)", head, re.I)
        return "drop_table", (_decode_ident(m.group(1)) if m else None)
    if upper.startswith(b"LOCK"):
        return "lock", None
    if upper.startswith(b"UNLOCK"):
        return "unlock", None
    if upper.startswith(b"SET"):
        return "set", None
    if upper.startswith(b"USE"):
        return "use", None
    return "other", None


class StatementReader:
    """Iterate over statements of a SQL file between two byte offsets.

    Memory use is bounded by the chunk size plus the largest single statement.
    """

    def __init__(self, path: str | Path, *, start: int = 0, end: int | None = None,
                 start_line: int = 1, chunk_size: int = 8 * 1024 * 1024,
                 max_statement_bytes: int = 2 * 1024 * 1024 * 1024):
        self.path = Path(path)
        self.start = start
        self.end = end
        self.start_line = start_line
        self.chunk_size = max(4096, chunk_size)
        self.max_statement_bytes = max_statement_bytes
        self.position = start          # file offset of the next unread statement
        self.line = start_line         # line number at `position`

    def __iter__(self) -> Iterator[Statement]:
        with open(self.path, "rb") as fh:
            fh.seek(self.start)
            buf = b""
            pos = 0                     # index in buf corresponding to self.position
            eof = False
            read_size = self.chunk_size
            while True:
                if self.end is not None and self.position >= self.end:
                    return
                m = _STATEMENT_RE.match(buf, pos) if pos < len(buf) else None
                if m is None:
                    if eof:
                        tail = buf[pos:]
                        lead = _LEADING_RE.match(tail)
                        if lead.end() < len(tail):
                            line = self.line + tail.count(b"\n", 0, lead.end())
                            raise SqlParseError(
                                "Incomplete statement at end of file (missing ';' or unterminated "
                                "string/comment)",
                                line=line, offset=self.position + lead.end(),
                                context=tail[lead.end():lead.end() + 120].decode("utf-8", "replace"),
                            )
                        return
                    pending = len(buf) - pos
                    if pending > self.max_statement_bytes:
                        raise SqlParseError(
                            f"Statement exceeds the maximum supported size of "
                            f"{self.max_statement_bytes} bytes (unterminated string or comment?)",
                            line=self.line, offset=self.position,
                        )
                    chunk = fh.read(read_size)
                    if not chunk:
                        eof = True
                    buf = buf[pos:] + chunk
                    pos = 0
                    # Grow reads geometrically for very large statements so the
                    # rescans stay amortised linear.
                    read_size = max(self.chunk_size, pending)
                    continue

                raw = buf[pos:m.end()]
                lead = _LEADING_RE.match(raw)
                body_start = lead.end()
                line = self.line + raw.count(b"\n", 0, body_start)
                stmt = Statement(raw=raw, offset=self.position, end_offset=self.position + len(raw),
                                 line=line, body_start=body_start)
                self.line += raw.count(b"\n")
                self.position += len(raw)
                pos = m.end()
                read_size = self.chunk_size
                if pos > self.chunk_size and pos > len(buf) // 2:
                    buf = buf[pos:]
                    pos = 0
                yield stmt


# --------------------------------------------------------------------------- #
# INSERT row parsing
# --------------------------------------------------------------------------- #

_VALUE_RE = re.compile(
    r"""
    \s*
    (?:
        '((?:[^'\\]++|\\.|'')*+)'                                   # 1 string
      | (NULL)(?![\w])                                               # 2 null
      | ([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)(?![\w.])    # 3 number
      | 0x([0-9A-Fa-f]*)(?![\w])                                     # 4 hex number
      | [xX]'([0-9A-Fa-f]*)'                                         # 5 hex string
      | _binary\s*'((?:[^'\\]++|\\.|'')*+)'                         # 6 binary string
      | [bB]'([01]*)'                                                # 7 bit string
      | (TRUE|FALSE)(?![\w])                                         # 8 boolean
      | _(?:utf8mb4|utf8mb3|utf8|latin1|ascii)\s*'((?:[^'\\]++|\\.|'')*+)'   # 9 introducer
    )
    \s*([,)])                                                        # 10 separator
    """,
    re.VERBOSE | re.DOTALL | re.IGNORECASE,
)
_ROW_OPEN_RE = re.compile(r"\s*\(")
_EMPTY_ROW_CLOSE_RE = re.compile(r"\s*\)")
_ROW_SEP_RE = re.compile(r"\s*(,|;|\Z)")
_ESC_RE = re.compile(r"\\(.)|''", re.DOTALL)
_ESC_MAP = {
    "0": "\x00", "'": "'", '"': '"', "b": "\b", "n": "\n", "r": "\r",
    "t": "\t", "Z": "\x1a", "\\": "\\", "%": "\\%", "_": "\\_",
}


def _esc_repl(m: re.Match) -> str:
    c = m.group(1)
    if c is None:
        return "'"
    return _ESC_MAP.get(c, c)


def unescape_mysql_string(s: str) -> str:
    """Decode the body of a single-quoted MySQL string literal."""
    if "\\" not in s and "''" not in s:
        return s
    return _ESC_RE.sub(_esc_repl, s)


def _to_bytes(s: str) -> bytes:
    return unescape_mysql_string(s).encode("utf-8", "surrogateescape")


class InvalidTextValue:
    """Placeholder for a text value that is not valid UTF-8 (row will be rejected)."""

    __slots__ = ("raw",)

    def __init__(self, raw: bytes):
        self.raw = raw

    def __repr__(self) -> str:
        return f"InvalidTextValue({self.raw[:40]!r})"


@dataclass
class InsertStatement:
    verb: str                     # insert | replace
    ignore: bool
    table: str
    columns: list[str] | None     # explicit column list, if present
    text: str                     # decoded statement text
    values_pos: int               # index in text where the row tuples start
    has_invalid_utf8: bool
    line: int
    offset: int

    def iter_rows(self) -> Iterator[tuple]:
        """Yield each row tuple. Raises SqlParseError on malformed input."""
        text = self.text
        pos = self.values_pos
        n = len(text)
        vmatch = _VALUE_RE.match
        while True:
            m = _ROW_OPEN_RE.match(text, pos)
            if not m:
                raise self._error("Expected '(' at start of row tuple", pos)
            pos = m.end()
            row: list = []
            # Empty tuple "()" is legal for tables with all-default columns.
            close = _EMPTY_ROW_CLOSE_RE.match(text, pos)
            if close:
                pos = close.end()
            else:
                while True:
                    m = vmatch(text, pos)
                    if m is None:
                        raise self._error("Unsupported or malformed value in row tuple", pos)
                    s = m.group(1)
                    if s is not None:
                        v = unescape_mysql_string(s)
                        if self.has_invalid_utf8 and _has_surrogates(v):
                            v = InvalidTextValue(v.encode("utf-8", "surrogateescape"))
                        row.append(v)
                    elif m.group(2) is not None:
                        row.append(None)
                    elif (num := m.group(3)) is not None:
                        if "." in num or "e" in num or "E" in num:
                            row.append(Decimal(num))
                        else:
                            row.append(int(num))
                    elif (hx := m.group(4)) is not None:
                        row.append(bytes.fromhex(hx if len(hx) % 2 == 0 else "0" + hx))
                    elif (hx := m.group(5)) is not None:
                        row.append(bytes.fromhex(hx))
                    elif (bs := m.group(6)) is not None:
                        row.append(_to_bytes(bs))
                    elif (bits := m.group(7)) is not None:
                        row.append(int(bits, 2) if bits else 0)
                    elif (b := m.group(8)) is not None:
                        row.append(1 if b.upper() == "TRUE" else 0)
                    else:
                        v = unescape_mysql_string(m.group(9))
                        if self.has_invalid_utf8 and _has_surrogates(v):
                            v = InvalidTextValue(v.encode("utf-8", "surrogateescape"))
                        row.append(v)
                    pos = m.end()
                    if m.group(10) == ")":
                        break
            yield tuple(row)
            m = _ROW_SEP_RE.match(text, pos)
            if not m:
                raise self._error(
                    "Unexpected content after row tuple (ON DUPLICATE KEY UPDATE and other "
                    "trailing clauses are not supported)", pos)
            pos = m.end()
            if m.group(1) != ",":
                return

    def _error(self, msg: str, pos: int) -> SqlParseError:
        line = self.line + self.text.count("\n", 0, pos)
        return SqlParseError(msg, line=line, offset=self.offset,
                             context=self.text[pos:pos + 80])


def _has_surrogates(s: str) -> bool:
    try:
        s.encode("utf-8")
        return False
    except UnicodeEncodeError:
        return True


def parse_insert(stmt: Statement) -> InsertStatement:
    """Parse the head of an INSERT/REPLACE statement (rows are parsed lazily)."""
    body = stmt.body
    m = _INSERT_HEAD_RE.match(body)
    if not m:
        raise SqlParseError("Unrecognised INSERT statement", line=stmt.line, offset=stmt.offset,
                            context=body[:120].decode("utf-8", "replace"))
    keyword = m.group(6).upper()
    if keyword not in (b"VALUES", b"VALUE"):
        raise SqlParseError(f"Unsupported INSERT form ({keyword.decode()} ...); only "
                            "INSERT ... VALUES is supported", line=stmt.line, offset=stmt.offset)
    columns = None
    if m.group(5):
        columns = [_decode_ident(t.group(0)) for t in _IDENT_RE.finditer(m.group(5))]
    try:
        text = body.decode("utf-8")
        invalid = False
    except UnicodeDecodeError:
        text = body.decode("utf-8", "surrogateescape")
        invalid = True
    # Head is ASCII-only up to the VALUES keyword, so byte and char offsets agree.
    values_pos = len(body[:m.end()].decode("utf-8", "surrogateescape"))
    return InsertStatement(
        verb=m.group(1).decode().lower(),
        ignore=bool(m.group(2)),
        table=_decode_ident(m.group(4)),
        columns=columns,
        text=text,
        values_pos=values_pos,
        has_invalid_utf8=invalid,
        line=stmt.line,
        offset=stmt.offset,
    )


def count_insert_rows(stmt: Statement) -> int:
    """Fast row count for an INSERT ... VALUES statement without full parsing."""
    body = stmt.body
    m = _INSERT_HEAD_RE.match(body)
    if not m or m.group(6).upper() not in (b"VALUES", b"VALUE"):
        return 0
    rest = _STRING_LITERAL_RE.sub(b"", body[m.end():])
    rest = rest.replace(b" ", b"").replace(b"\n", b"").replace(b"\r", b"").replace(b"\t", b"")
    if not rest.startswith(b"("):
        return 0
    return rest.count(b"),(") + 1


# --------------------------------------------------------------------------- #
# CREATE TABLE inspection (read-only; never executed)
# --------------------------------------------------------------------------- #

_COLUMN_DEF_RE = re.compile(rb"^[ \t]*`((?:[^`]|``)+)`[ \t]+([^\n]*?),?[ \t\r]*$", re.MULTILINE)
_COLLATE_RE = re.compile(rb"COLLATE\s*=?\s*([A-Za-z0-9_]+)", re.IGNORECASE)
_CHARSET_RE = re.compile(rb"(?:DEFAULT\s+)?(?:CHARSET|CHARACTER\s+SET)\s*=?\s*([A-Za-z0-9_]+)", re.IGNORECASE)


@dataclass
class CreateTableInfo:
    table: str
    columns: list[str]
    collations: list[str]
    charsets: list[str]
    column_definitions: dict[str, str]


def parse_create_table(stmt: Statement) -> CreateTableInfo:
    body = stmt.body
    m = _CREATE_TABLE_RE.match(body)
    if not m:
        raise SqlParseError("Unrecognised CREATE TABLE", line=stmt.line, offset=stmt.offset)
    table = _decode_ident(m.group(1))
    open_idx = body.find(b"(", m.end())
    close_idx = body.rfind(b")")
    inner = body[open_idx + 1:close_idx] if open_idx != -1 and close_idx > open_idx else b""
    columns: list[str] = []
    definitions: dict[str, str] = {}
    for c in _COLUMN_DEF_RE.finditer(inner):
        name = c.group(1).replace(b"``", b"`").decode("utf-8", "replace")
        columns.append(name)
        definitions[name] = c.group(2).decode("utf-8", "replace").strip()
    collations = sorted({c.decode() for c in _COLLATE_RE.findall(body)})
    charsets = sorted({c.decode() for c in _CHARSET_RE.findall(body)})
    return CreateTableInfo(table=table, columns=columns, collations=collations, charsets=charsets,
                           column_definitions=definitions)
