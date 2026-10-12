"""Shared fixtures. Integration tests use a disposable database, never `tawos`."""

from __future__ import annotations

import hashlib
import os
import sys
import threading
from pathlib import Path

import pymysql
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.db import Database  # noqa: E402
from app.importer import TableImporter  # noqa: E402
from app.job_store import JobStore  # noqa: E402
from app.source_index import SourceIndexManager  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
SAMPLE_DUMP = FIXTURES / "sample_dump.sql"
TEST_SCHEMA = FIXTURES / "test_schema.sql"

TEST_DB = os.getenv("TEST_DB_NAME", "tawos_importer_test")
DB_HOST = os.getenv("TEST_DB_HOST", os.getenv("DB_HOST", "localhost"))
DB_PORT = int(os.getenv("TEST_DB_PORT", os.getenv("DB_PORT", "3306")))
DB_USER = os.getenv("TEST_DB_USER", os.getenv("DB_USER", "root"))
DB_PASSWORD = os.getenv("TEST_DB_PASSWORD", os.getenv("DB_PASSWORD", ""))

DELETE_ORDER = ["issue_link", "issue_component", "fix_version", "affected_version", "change_log", "comment",
                "issue", "sprint", "component", "version", "user", "project", "repository"]

assert TEST_DB != "tawos", "Integration tests must not run against the production database"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _server_conn(database: str | None = None):
    return pymysql.connect(host=DB_HOST, port=DB_PORT, user=DB_USER, password=DB_PASSWORD,
                           database=database, charset="utf8mb4", autocommit=True, connect_timeout=3)


@pytest.fixture(scope="session")
def test_database():
    try:
        conn = _server_conn()
    except pymysql.err.MySQLError as exc:
        pytest.skip(f"MySQL/MariaDB not available for integration tests: {exc}")
    with conn.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS `{TEST_DB}`")
        cur.execute(f"CREATE DATABASE `{TEST_DB}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
        cur.execute(f"USE `{TEST_DB}`")
        for stmt in TEST_SCHEMA.read_text(encoding="utf-8").split(";"):
            body = "\n".join(l for l in stmt.splitlines() if not l.strip().startswith("--")).strip()
            if body:
                cur.execute(body)
    conn.close()
    yield TEST_DB
    conn = _server_conn()
    with conn.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS `{TEST_DB}`")
    conn.close()


@pytest.fixture()
def db_conn(test_database):
    conn = _server_conn(test_database)
    with conn.cursor() as cur:
        for t in DELETE_ORDER:
            cur.execute(f"DELETE FROM `{t}`")
    yield conn
    conn.close()


def make_settings(tmp_path: Path, **overrides) -> Settings:
    base = dict(sql_path=SAMPLE_DUMP, db_host=DB_HOST, db_port=DB_PORT, db_name=TEST_DB, db_user=DB_USER,
                db_password=DB_PASSWORD, state_dir=tmp_path / "state", log_dir=tmp_path / "logs",
                batch_size=2, disk_space_reserve_gb=0.0, auto_build_index=True, reader_chunk_bytes=4096,
                ignore_source_columns={"project": frozenset({"sp_field_id"})})
    base.update(overrides)
    return Settings(**base)


class Components:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = JobStore(settings.state_db_path)
        self.db = Database(settings)
        self.index = SourceIndexManager(settings.sql_path, self.store, settings.reader_chunk_bytes)
        if not self.index.load_cached():
            self.index.start_build()
        self.index.wait_ready(timeout=60)

    def run_import(self, table: str, *, duplicate_policy="skip_existing", error_policy="continue",
                   batch_size: int | None = None, checkpoint_job: dict | None = None,
                   cancel_after_batches: int | None = None) -> dict:
        cp = checkpoint_job.get("checkpoint") if checkpoint_job else None
        job = self.store.create_job(table, duplicate_policy=duplicate_policy, error_policy=error_policy,
                                    batch_size=batch_size or self.settings.batch_size,
                                    resumed_from=checkpoint_job["id"] if checkpoint_job else None,
                                    checkpoint=cp, base_ordinal=(cp or {}).get("ordinal") or 0)
        cancel = threading.Event()
        importer = TableImporter(self.settings, self.db, self.index, self.store, job, cancel, {})
        if cancel_after_batches is not None:
            original = importer._save_checkpoint

            def save_and_maybe_cancel(cp_tuple):
                original(cp_tuple)
                if importer.counters["batches_committed"] >= cancel_after_batches:
                    cancel.set()
            importer._save_checkpoint = save_and_maybe_cancel
        importer.run()
        return self.store.get_job(job["id"])

    def close(self):
        self.db.dispose()
        self.store.close()


@pytest.fixture()
def components(tmp_path, db_conn):
    c = Components(make_settings(tmp_path))
    yield c
    c.close()


def import_parents(c: Components, *tables: str) -> None:
    for t in tables:
        job = c.run_import(t)
        assert job["status"] in ("completed", "completed_with_errors"), job["error"]
