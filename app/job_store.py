"""Persistent import-job history, row-level failures and the source-index cache.

Stored in a local SQLite file so nothing is written into the target database
except the imported rows themselves.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ACTIVE_STATUSES = ("queued", "running", "cancelling")
RESUMABLE_STATUSES = ("failed", "cancelled", "interrupted")
TERMINAL_STATUSES = ("completed", "completed_with_errors", "failed", "cancelled", "interrupted")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    table_name TEXT NOT NULL,
    status TEXT NOT NULL,
    stage TEXT,
    duplicate_policy TEXT NOT NULL,
    error_policy TEXT NOT NULL,
    batch_size INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    total_rows INTEGER,
    processed INTEGER NOT NULL DEFAULT 0,
    inserted INTEGER NOT NULL DEFAULT 0,
    updated INTEGER NOT NULL DEFAULT 0,
    skipped INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    batches_committed INTEGER NOT NULL DEFAULT 0,
    base_ordinal INTEGER NOT NULL DEFAULT 0,
    bytes_start INTEGER,
    bytes_end INTEGER,
    bytes_pos INTEGER,
    checkpoint_offset INTEGER,
    checkpoint_line INTEGER,
    checkpoint_row INTEGER,
    checkpoint_ordinal INTEGER,
    resumed_from TEXT,
    error TEXT,
    warnings TEXT,
    duration_seconds REAL
);
CREATE INDEX IF NOT EXISTS ix_jobs_table ON jobs(table_name, created_at);

CREATE TABLE IF NOT EXISTS job_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    stage TEXT,
    kind TEXT,
    line INTEGER,
    row_in_statement INTEGER,
    row_key TEXT,
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_job_errors_job ON job_errors(job_id, id);

CREATE TABLE IF NOT EXISTS source_index (
    path TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    built_at TEXT NOT NULL,
    data TEXT NOT NULL
);
"""

_JOB_FIELDS = (
    "status", "stage", "started_at", "finished_at", "total_rows", "processed", "inserted",
    "updated", "skipped", "failed", "batches_committed", "base_ordinal", "bytes_start",
    "bytes_end", "bytes_pos", "checkpoint_offset", "checkpoint_line", "checkpoint_row",
    "checkpoint_ordinal", "error", "warnings", "duration_seconds",
)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class JobStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ jobs
    def create_job(self, table: str, *, duplicate_policy: str, error_policy: str, batch_size: int,
                   resumed_from: str | None = None, checkpoint: dict | None = None,
                   base_ordinal: int = 0) -> dict:
        job_id = uuid.uuid4().hex[:12]
        cp = checkpoint or {}
        with self._lock:
            self._conn.execute(
                "INSERT INTO jobs (id, table_name, status, stage, duplicate_policy, error_policy, batch_size,"
                " created_at, resumed_from, checkpoint_offset, checkpoint_line, checkpoint_row,"
                " checkpoint_ordinal, base_ordinal) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, table, "queued", "queued", duplicate_policy, error_policy, batch_size, utcnow(),
                 resumed_from, cp.get("offset"), cp.get("line"), cp.get("row"), cp.get("ordinal"),
                 base_ordinal),
            )
        return self.get_job(job_id)

    def update_job(self, job_id: str, **fields: Any) -> None:
        bad = set(fields) - set(_JOB_FIELDS)
        if bad:
            raise ValueError(f"Unknown job fields: {bad}")
        if "warnings" in fields and not isinstance(fields["warnings"], (str, type(None))):
            fields["warnings"] = json.dumps(fields["warnings"])
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self._lock:
            self._conn.execute(f"UPDATE jobs SET {cols} WHERE id = ?", (*fields.values(), job_id))

    def get_job(self, job_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _job_dict(row) if row else None

    def list_jobs(self, limit: int = 100, table: str | None = None) -> list[dict]:
        with self._lock:
            if table:
                rows = self._conn.execute(
                    "SELECT * FROM jobs WHERE table_name = ? ORDER BY created_at DESC, rowid DESC LIMIT ?",
                    (table, limit)).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM jobs ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)).fetchall()
        return [_job_dict(r) for r in rows]

    def latest_job_per_table(self) -> dict[str, dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT j.* FROM jobs j JOIN (SELECT table_name, MAX(rowid) AS rid FROM jobs GROUP BY table_name) m"
                " ON j.rowid = m.rid").fetchall()
        return {r["table_name"]: _job_dict(r) for r in rows}

    def mark_interrupted_jobs(self) -> int:
        """Called at startup: jobs left active by a previous process are interrupted."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE jobs SET status = 'interrupted', stage = 'interrupted', finished_at = ?,"
                " error = COALESCE(error, 'The application stopped while this job was running. '"
                " || 'Resume it to continue from the last committed batch.')"
                f" WHERE status IN ({','.join('?' * len(ACTIVE_STATUSES))})",
                (utcnow(), *ACTIVE_STATUSES))
            return cur.rowcount

    # ------------------------------------------------------------------ errors
    def add_errors(self, job_id: str, errors: list[dict]) -> None:
        if not errors:
            return
        now = utcnow()
        with self._lock:
            self._conn.executemany(
                "INSERT INTO job_errors (job_id, created_at, stage, kind, line, row_in_statement, row_key, message)"
                " VALUES (?,?,?,?,?,?,?,?)",
                [(job_id, now, e.get("stage"), e.get("kind"), e.get("line"), e.get("row_in_statement"),
                  e.get("row_key"), e["message"][:4000]) for e in errors])

    def get_errors(self, job_id: str, limit: int = 200, offset: int = 0) -> tuple[list[dict], int]:
        with self._lock:
            total = self._conn.execute("SELECT COUNT(*) FROM job_errors WHERE job_id = ?", (job_id,)).fetchone()[0]
            rows = self._conn.execute(
                "SELECT * FROM job_errors WHERE job_id = ? ORDER BY id LIMIT ? OFFSET ?",
                (job_id, limit, offset)).fetchall()
        return [dict(r) for r in rows], total

    def error_count(self, job_id: str) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM job_errors WHERE job_id = ?", (job_id,)).fetchone()[0]

    # ------------------------------------------------------------------ source index cache
    def load_index(self, path: str, size: int, mtime: float) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM source_index WHERE path = ?", (path,)).fetchone()
        if row and row["size"] == size and abs(row["mtime"] - mtime) < 1e-3:
            return json.loads(row["data"])
        return None

    def save_index(self, path: str, size: int, mtime: float, data: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO source_index (path, size, mtime, built_at, data) VALUES (?,?,?,?,?)",
                (path, size, mtime, utcnow(), json.dumps(data)))

    def clear_index(self, path: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM source_index WHERE path = ?", (path,))


def _job_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["warnings"] = json.loads(d["warnings"]) if d.get("warnings") else []
    d["checkpoint"] = None
    if d.get("checkpoint_offset") is not None:
        d["checkpoint"] = {"offset": d["checkpoint_offset"], "line": d["checkpoint_line"],
                           "row": d["checkpoint_row"], "ordinal": d["checkpoint_ordinal"]}
    return d
