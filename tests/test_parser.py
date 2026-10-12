from decimal import Decimal

import pytest

from app.sql_parser import (InvalidTextValue, SqlParseError, StatementReader, classify, count_insert_rows,
                            parse_create_table, parse_insert, unescape_mysql_string)
from conftest import SAMPLE_DUMP


def _write(tmp_path, content: bytes):
    p = tmp_path / "t.sql"
    p.write_bytes(content)
    return p


def _statements(path, chunk):
    return list(StatementReader(path, chunk_size=chunk))


def test_reader_is_independent_of_buffer_boundaries():
    """Tiny chunks force every construct to be split across reads."""
    big = _statements(SAMPLE_DUMP, 8 * 1024 * 1024)
    for chunk in (4096, 4097, 5000):
        small = _statements(SAMPLE_DUMP, chunk)
        assert [(s.offset, s.raw) for s in small] == [(s.offset, s.raw) for s in big]


def test_reader_respects_strings_and_comments(tmp_path):
    sql = (b"-- leading ; comment\n"
           b"/* block ; comment */\n"
           b"INSERT INTO `t` VALUES (1,'a;b'),(2,'it''s; \\'x\\' ;'),(3,'--not; comment'),(4,'/*;*/');\n"
           b"# hash ; comment\n"
           b"INSERT INTO `t` VALUES (5,'line1\nline2;');\n")
    p = _write(tmp_path, sql)
    stmts = [s for s in StatementReader(p, chunk_size=4096)]
    assert len(stmts) == 2
    assert stmts[0].line == 3
    assert stmts[1].line == 5
    rows = list(parse_insert(stmts[0]).iter_rows())
    assert rows == [(1, "a;b"), (2, "it's; 'x' ;"), (3, "--not; comment"), (4, "/*;*/")]
    assert list(parse_insert(stmts[1]).iter_rows()) == [(5, "line1\nline2;")]


def test_reader_offsets_allow_seeking(tmp_path):
    p = SAMPLE_DUMP
    stmts = _statements(p, 4096)
    target = [s for s in stmts if classify(s) == ("insert", "Issue")][1]
    resumed = next(iter(StatementReader(p, start=target.offset, start_line=target.offset_line)))
    assert resumed.raw == target.raw
    assert resumed.line == target.line


def test_classification_of_dump_statements():
    kinds = {}
    for s in _statements(SAMPLE_DUMP, 65536):
        k, _ = classify(s)
        kinds[k] = kinds.get(k, 0) + 1
    assert kinds["create_table"] == 13
    assert kinds["drop_table"] == 13
    assert kinds["insert"] == 17
    assert kinds["conditional_comment"] > 10
    assert kinds["lock"] == 13 and kinds["unlock"] == 13


def test_lf_and_crlf_dumps_parse_identically(tmp_path):
    raw = SAMPLE_DUMP.read_bytes().replace(b"\r\n", b"\n")
    lf = _write(tmp_path, raw)
    crlf = tmp_path / "crlf.sql"
    crlf.write_bytes(raw.replace(b"\n", b"\r\n"))
    a = [(classify(s), s.line) for s in StatementReader(lf, chunk_size=4096)]
    b = [(classify(s), s.line) for s in StatementReader(crlf, chunk_size=4096)]
    assert a == b
    assert ("other", None) not in [k for k, _ in a]


def test_values_and_escapes(tmp_path):
    sql = (b"INSERT INTO `x` VALUES (NULL,-12,3.50,1.5e3,'\\0\\b\\n\\r\\t\\Z\\\\\\%\\_\\q',"
           b"0x4142,X'4344',_binary 'b\\'in',b'101',TRUE,_utf8mb4'u',''),"
           b"( 7 , 'spaced' , NULL , 0 , '' , 0x , X'' , _binary'' , b'' , FALSE , _utf8'v' , 'w' );")
    stmt = next(iter(StatementReader(_write(tmp_path, sql))))
    rows = list(parse_insert(stmt).iter_rows())
    assert rows[0] == (None, -12, Decimal("3.50"), Decimal("1.5e3"), "\x00\b\n\r\t\x1a\\\\%\\_q",
                       b"AB", b"CD", b"b'in", 5, 1, "u", "")
    assert rows[1] == (7, "spaced", None, 0, "", b"", b"", b"", 0, 0, "v", "w")


def test_unescape_keeps_like_escapes():
    assert unescape_mysql_string(r"50\% off \_x") == r"50\% off \_x"
    assert unescape_mysql_string("a''b") == "a'b"


def test_unicode_and_multibyte(tmp_path):
    sql = "INSERT INTO `x` VALUES (1,'émoji 🚀 日本語 ;'),(2,'\\'🚀\\'');\n".encode("utf-8")
    stmt = next(iter(StatementReader(_write(tmp_path, sql))))
    assert list(parse_insert(stmt).iter_rows()) == [(1, "émoji 🚀 日本語 ;"), (2, "'🚀'")]


def test_invalid_utf8_is_flagged_not_mangled(tmp_path):
    sql = b"INSERT INTO `x` VALUES (1,'ok'),(2,'bad \xff byte');\n"
    stmt = next(iter(StatementReader(_write(tmp_path, sql))))
    rows = list(parse_insert(stmt).iter_rows())
    assert rows[0] == (1, "ok")
    assert isinstance(rows[1][1], InvalidTextValue)
    assert rows[1][1].raw == b"bad \xff byte"


def test_explicit_column_list_and_insert_ignore(tmp_path):
    sql = b"INSERT IGNORE INTO `db`.`Tbl` (`a`, `b c`, d) VALUES (1,'x',NULL);"
    stmt = next(iter(StatementReader(_write(tmp_path, sql))))
    assert classify(stmt) == ("insert", "Tbl")
    ins = parse_insert(stmt)
    assert ins.ignore and ins.columns == ["a", "b c", "d"] and ins.table == "Tbl"
    assert list(ins.iter_rows()) == [(1, "x", None)]


def test_count_rows_matches_parser():
    for s in _statements(SAMPLE_DUMP, 65536):
        if classify(s)[0] == "insert":
            assert count_insert_rows(s) == len(list(parse_insert(s).iter_rows()))


def test_large_statement_spanning_many_chunks(tmp_path):
    big_text = ("x;'),(\\'" * 50_000)
    escaped = big_text.replace("\\", "\\\\").replace("'", "\\'")
    sql = f"INSERT INTO `t` VALUES (1,'{escaped}'),(2,'tail');\nINSERT INTO `t` VALUES (3,'z');\n"
    p = _write(tmp_path, sql.encode())
    stmts = list(StatementReader(p, chunk_size=4096))
    assert len(stmts) == 2
    rows = list(parse_insert(stmts[0]).iter_rows())
    assert rows[0] == (1, big_text) and rows[1] == (2, "tail")
    assert count_insert_rows(stmts[0]) == 2


def test_unterminated_string_reports_location(tmp_path):
    p = _write(tmp_path, b"INSERT INTO `t` VALUES (1,'ok');\nINSERT INTO `t` VALUES (2,'never closed);\n")
    with pytest.raises(SqlParseError) as exc:
        list(StatementReader(p, chunk_size=4096))
    assert exc.value.line == 2


def test_malformed_value_reports_line(tmp_path):
    p = _write(tmp_path, b"\n\nINSERT INTO `t` VALUES (1,'a'),(2,NOW());\n")
    stmt = next(iter(StatementReader(p)))
    gen = parse_insert(stmt).iter_rows()
    assert next(gen) == (1, "a")
    with pytest.raises(SqlParseError) as exc:
        next(gen)
    assert exc.value.line == 3
    assert "NOW()" in exc.value.context


def test_unsupported_insert_forms(tmp_path):
    p = _write(tmp_path, b"INSERT INTO t SELECT * FROM u;\nINSERT INTO t VALUES (1) ON DUPLICATE KEY UPDATE a=1;\n")
    s1, s2 = list(StatementReader(p))
    with pytest.raises(SqlParseError, match="Unsupported INSERT form"):
        parse_insert(s1)
    with pytest.raises(SqlParseError, match="trailing clauses"):
        list(parse_insert(s2).iter_rows())


def test_parse_create_table():
    stmt = [s for s in _statements(SAMPLE_DUMP, 65536) if classify(s) == ("create_table", "Repository")][0]
    info = parse_create_table(stmt)
    assert info.columns == ["ID", "Name", "Description", "URL"]
    assert "utf8mb4_0900_ai_ci" in info.collations and "utf8_general_ci" in info.collations
