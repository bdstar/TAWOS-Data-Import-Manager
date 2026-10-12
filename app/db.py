"""Target database access: connections, schema inspection, dependency ordering."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

import pymysql
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Engine

from .config import Settings

log = logging.getLogger("tawos.db")


def quote_ident(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def build_engine(settings: Settings) -> Engine:
    url = URL.create(
        "mysql+pymysql",
        username=settings.db_user,
        password=settings.db_password or None,
        host=settings.db_host,
        port=settings.db_port,
        database=settings.db_name,
        query={"charset": "utf8mb4"},
    )
    return create_engine(url, pool_pre_ping=True, pool_recycle=1800, pool_size=5, max_overflow=5,
                         connect_args={"connect_timeout": 5})


def open_import_connection(settings: Settings) -> pymysql.connections.Connection:
    """Dedicated DB-API connection for bulk inserts (explicit transactions)."""
    conn = pymysql.connect(
        host=settings.db_host,
        port=settings.db_port,
        user=settings.db_user,
        password=settings.db_password,
        database=settings.db_name,
        charset="utf8mb4",
        autocommit=False,
        connect_timeout=10,
        read_timeout=3600,
        write_timeout=3600,
    )
    apply_session_settings(conn, settings)
    return conn


def apply_session_settings(conn, settings: Settings) -> None:
    with conn.cursor() as cur:
        cur.execute("SET NAMES utf8mb4 COLLATE utf8mb4_unicode_ci")
        cur.execute("SET SESSION sql_mode = %s", (settings.session_sql_mode,))
        cur.execute("SET SESSION time_zone = %s", (settings.session_time_zone,))
        # Integrity checks stay enabled: foreign keys and unique keys are enforced.
        cur.execute("SET SESSION foreign_key_checks = 1, unique_checks = 1")
    conn.commit()


def describe_db_error(exc: BaseException) -> str:
    """Human readable DB error that never includes credentials."""
    if isinstance(exc, pymysql.err.MySQLError) and exc.args:
        code = exc.args[0]
        msg = exc.args[1] if len(exc.args) > 1 else ""
        return f"MySQL error {code}: {msg}"
    orig = getattr(exc, "orig", None)
    if orig is not None and orig is not exc:
        return describe_db_error(orig)
    return f"{type(exc).__name__}: {exc}"


@dataclass
class ForeignKey:
    name: str
    table: str
    columns: list[str]
    ref_table: str
    ref_columns: list[str]


@dataclass
class TableSchema:
    name: str
    columns: list[str]
    primary_key: list[str]
    unique_keys: list[list[str]]
    foreign_keys: list[ForeignKey] = field(default_factory=list)

    @property
    def key_columns(self) -> list[str]:
        """Columns identifying a record for duplicate detection."""
        if self.primary_key:
            return self.primary_key
        if self.unique_keys:
            return self.unique_keys[0]
        return list(self.columns)

    @property
    def has_real_key(self) -> bool:
        return bool(self.primary_key or self.unique_keys)

    def column_lookup(self) -> dict[str, str]:
        return {c.lower(): c for c in self.columns}


@dataclass
class ServerInfo:
    version: str
    is_mariadb: bool
    max_allowed_packet: int
    datadir: str
    lower_case_table_names: int
    supported_collations: set[str]


class Database:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.engine = build_engine(settings)
        self._schema_lock = threading.Lock()
        self._schema: dict[str, TableSchema] | None = None
        self._server: ServerInfo | None = None
        self._counts_lock = threading.Lock()
        self._counts: dict[str, int] = {}
        self._counts_updated: float | None = None
        self._counts_refreshing: set[str] = set()

    # ------------------------------------------------------------------ status
    def ping(self) -> tuple[bool, str | None]:
        try:
            with self.engine.connect() as c:
                c.execute(text("SELECT 1"))
            return True, None
        except Exception as exc:  # noqa: BLE001
            return False, describe_db_error(exc)

    def server_info(self, refresh: bool = False) -> ServerInfo:
        if self._server is not None and not refresh:
            return self._server
        with self.engine.connect() as c:
            version = c.execute(text("SELECT VERSION()")).scalar()
            packet = int(c.execute(text("SELECT @@max_allowed_packet")).scalar())
            datadir = c.execute(text("SELECT @@datadir")).scalar()
            lctn = int(c.execute(text("SELECT @@lower_case_table_names")).scalar())
            colls = {r[0] for r in c.execute(text("SELECT COLLATION_NAME FROM information_schema.COLLATIONS"))}
        self._server = ServerInfo(version=version, is_mariadb="mariadb" in version.lower(),
                                  max_allowed_packet=packet, datadir=datadir,
                                  lower_case_table_names=lctn, supported_collations=colls)
        return self._server

    # ------------------------------------------------------------------ schema
    def schema(self, refresh: bool = False) -> dict[str, TableSchema]:
        with self._schema_lock:
            if self._schema is None or refresh:
                self._schema = self._load_schema()
            return self._schema

    def _load_schema(self) -> dict[str, TableSchema]:
        db = self.settings.db_name
        with self.engine.connect() as c:
            cols = c.execute(text(
                "SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = :db ORDER BY TABLE_NAME, ORDINAL_POSITION"), {"db": db}).all()
            keys = c.execute(text(
                "SELECT TABLE_NAME, INDEX_NAME, COLUMN_NAME FROM information_schema.STATISTICS "
                "WHERE TABLE_SCHEMA = :db AND NON_UNIQUE = 0 ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX"),
                {"db": db}).all()
            fks = c.execute(text(
                "SELECT TABLE_NAME, CONSTRAINT_NAME, COLUMN_NAME, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME "
                "FROM information_schema.KEY_COLUMN_USAGE WHERE TABLE_SCHEMA = :db "
                "AND REFERENCED_TABLE_NAME IS NOT NULL ORDER BY TABLE_NAME, CONSTRAINT_NAME, ORDINAL_POSITION"),
                {"db": db}).all()
        tables: dict[str, TableSchema] = {}
        for tname, cname in cols:
            t = tables.setdefault(tname.lower(), TableSchema(tname, [], [], []))
            t.columns.append(cname)
        uniques: dict[tuple[str, str], list[str]] = {}
        for tname, iname, cname in keys:
            uniques.setdefault((tname.lower(), iname), []).append(cname)
        for (tname, iname), columns in uniques.items():
            if tname not in tables:
                continue
            if iname == "PRIMARY":
                tables[tname].primary_key = columns
            else:
                tables[tname].unique_keys.append(columns)
        fk_map: dict[tuple[str, str], ForeignKey] = {}
        for tname, cname, col, rtable, rcol in fks:
            key = (tname.lower(), cname)
            fk = fk_map.get(key)
            if fk is None:
                fk = ForeignKey(cname, tname.lower(), [], rtable.lower(), [])
                fk_map[key] = fk
                if tname.lower() in tables:
                    tables[tname.lower()].foreign_keys.append(fk)
            fk.columns.append(col)
            fk.ref_columns.append(rcol)
        return tables

    def parents_of(self, table: str) -> list[str]:
        t = self.schema().get(table)
        if not t:
            return []
        return sorted({fk.ref_table for fk in t.foreign_keys if fk.ref_table != table})

    def children_of(self, table: str) -> list[str]:
        return sorted({name for name, t in self.schema().items()
                       for fk in t.foreign_keys if fk.ref_table == table and name != table})

    def import_order(self, tables: list[str] | None = None) -> list[str]:
        """Topological order (parents first) derived from the live FK constraints."""
        schema = self.schema()
        names = [t for t in (tables or sorted(schema)) if t in schema]
        deps = {t: set(p for p in self.parents_of(t) if p in names) for t in names}
        order: list[str] = []
        ready = sorted(t for t, d in deps.items() if not d)
        while ready:
            t = ready.pop(0)
            order.append(t)
            for other in sorted(deps):
                if t in deps[other]:
                    deps[other].discard(t)
                    if not deps[other] and other not in order and other not in ready:
                        ready.append(other)
            ready.sort()
        remaining = [t for t in names if t not in order]
        if remaining:
            log.warning("Foreign-key cycle detected among: %s", remaining)
            order.extend(sorted(remaining))
        return order

    def dependency_levels(self) -> dict[str, int]:
        levels: dict[str, int] = {}
        for t in self.import_order():
            parents = self.parents_of(t)
            levels[t] = 1 + max((levels.get(p, 0) for p in parents), default=0)
        return levels

    # ------------------------------------------------------------------ counts
    def count_rows(self, table: str) -> int:
        if table not in self.schema():
            raise ValueError(f"Unknown table {table!r}")
        with self.engine.connect() as c:
            return int(c.execute(text(f"SELECT COUNT(*) FROM {quote_ident(self.schema()[table].name)}")).scalar())

    def has_rows(self, table: str) -> bool:
        with self.engine.connect() as c:
            return c.execute(text(f"SELECT 1 FROM {quote_ident(self.schema()[table].name)} LIMIT 1")).first() is not None

    def refresh_counts(self, tables: list[str] | None = None) -> dict[str, int]:
        names = tables or list(self.schema())
        result = {}
        for t in names:
            with self._counts_lock:
                if t in self._counts_refreshing:
                    continue
                self._counts_refreshing.add(t)
            try:
                result[t] = self.count_rows(t)
                with self._counts_lock:
                    self._counts[t] = result[t]
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not count rows in %s: %s", t, describe_db_error(exc))
            finally:
                with self._counts_lock:
                    self._counts_refreshing.discard(t)
        with self._counts_lock:
            self._counts_updated = time.time()
        return result

    def refresh_counts_async(self, tables: list[str] | None = None) -> None:
        threading.Thread(target=self.refresh_counts, args=(tables,), name="count-refresh",
                         daemon=True).start()

    def cached_counts(self) -> tuple[dict[str, int], float | None, set[str]]:
        with self._counts_lock:
            return dict(self._counts), self._counts_updated, set(self._counts_refreshing)

    def dispose(self) -> None:
        self.engine.dispose()
