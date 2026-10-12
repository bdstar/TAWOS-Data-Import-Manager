"""One-pass index of the SQL dump: per-table byte ranges, row counts, columns.

Built in a background thread and cached (keyed by path, size and mtime) so it
only needs to run once per dump file. The dump is opened read-only.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from pathlib import Path

from .job_store import JobStore
from .sql_parser import (SqlParseError, StatementReader, classify, count_insert_rows,
                         parse_create_table)

log = logging.getLogger("tawos.index")

INDEX_VERSION = 3
IGNORED_KINDS = {"conditional_comment", "lock", "unlock", "set", "drop_table", "create_table"}
NOTABLE_KINDS = {"create_database", "drop_database", "use", "other", "insert_unparsed"}


def inspect_file(path: Path) -> dict:
    info = {"path": str(path), "exists": False, "readable": False, "is_file": False,
            "size": None, "mtime": None, "error": None}
    try:
        if not path.exists():
            info["error"] = f"File not found: {path}"
            return info
        info["exists"] = True
        if not path.is_file():
            info["error"] = f"Path is not a regular file: {path}"
            return info
        info["is_file"] = True
        st = path.stat()
        info["size"] = st.st_size
        info["mtime"] = st.st_mtime
        with open(path, "rb") as fh:
            fh.read(1)
        info["readable"] = True
    except PermissionError:
        info["error"] = f"File is not readable (permission denied): {path}"
    except OSError as exc:
        info["error"] = f"Cannot access file: {exc}"
    return info


def _parse_header(path: Path) -> dict:
    with open(path, "rb") as fh:
        head = fh.read(4096).decode("utf-8", "replace")
    out = {}
    if m := re.search(r"--\s*MySQL dump\s+(\S+)\s+Distrib\s+(\S+)", head):
        out["dump_tool"] = f"mysqldump {m.group(1)} (distrib {m.group(2)})"
    if m := re.search(r"--\s*Server version\s+(\S+)", head):
        out["source_server_version"] = m.group(1)
    if m := re.search(r"Database:\s*(\S+)", head):
        out["source_database"] = m.group(1)
    return out


def build_index(path: Path, *, chunk_size: int = 8 * 1024 * 1024, progress=None,
                cancel: threading.Event | None = None) -> dict:
    started = time.time()
    tables: dict[str, dict] = {}
    kinds: dict[str, int] = {}
    notable: list[dict] = []
    reader = StatementReader(path, chunk_size=chunk_size)
    last_report = 0.0
    for stmt in reader:
        kind, table = classify(stmt)
        kinds[kind] = kinds.get(kind, 0) + 1
        if kind in ("insert", "replace") and table:
            key = table.lower()
            t = tables.setdefault(key, _empty_table(table))
            if t["data_start"] is None:
                t["data_start"] = stmt.offset
                t["data_start_line"] = stmt.offset_line
                t["first_insert_line"] = stmt.line
            t["data_end"] = stmt.end_offset
            t["data_end_line"] = stmt.line
            t["insert_statements"] += 1
            t["bytes"] += len(stmt.raw)
            t["rows"] += count_insert_rows(stmt)
        elif kind == "create_table" and table:
            info = parse_create_table(stmt)
            t = tables.setdefault(table.lower(), _empty_table(table))
            t["create_columns"] = info.columns
            t["column_definitions"] = info.column_definitions
            t["collations"] = info.collations
            t["charsets"] = info.charsets
            t["create_line"] = stmt.line
        elif kind in NOTABLE_KINDS and len(notable) < 100:
            notable.append({"line": stmt.line, "kind": kind,
                            "snippet": stmt.head(160).decode("utf-8", "replace")})
        now = time.time()
        if progress and now - last_report > 0.5:
            progress(reader.position)
            last_report = now
        if cancel is not None and cancel.is_set():
            raise InterruptedError("Index build cancelled")
    if progress:
        progress(reader.position)
    return {
        "version": INDEX_VERSION,
        "header": _parse_header(path),
        "tables": tables,
        "statement_counts": kinds,
        "notable_statements": notable,
        "build_seconds": round(time.time() - started, 1),
    }


def _empty_table(dump_name: str) -> dict:
    return {"dump_name": dump_name, "create_columns": None, "column_definitions": {}, "collations": [],
            "charsets": [],
            "create_line": None, "data_start": None, "data_start_line": None,
            "first_insert_line": None, "data_end": None,
            "data_end_line": None, "insert_statements": 0, "rows": 0, "bytes": 0}


class SourceIndexManager:
    """Thread-safe owner of the source index and its background build."""

    def __init__(self, path: Path, store: JobStore, chunk_size: int):
        self.path = path
        self.store = store
        self.chunk_size = chunk_size
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._data: dict | None = None
        self.state = "missing"
        self.error: str | None = None
        self.progress_bytes = 0
        self.total_bytes = 0
        self.started_at: float | None = None
        self._thread: threading.Thread | None = None

    def load_cached(self) -> bool:
        info = inspect_file(self.path)
        if not info["readable"]:
            self.state, self.error = "error", info["error"]
            return False
        data = self.store.load_index(str(self.path), info["size"], info["mtime"])
        if data and data.get("version") == INDEX_VERSION:
            with self._lock:
                self._data = data
                self.state = "ready"
                self.progress_bytes = self.total_bytes = info["size"]
            self._ready.set()
            return True
        return False

    def start_build(self, force: bool = False) -> None:
        with self._lock:
            if self.state == "building":
                return
            if self.state == "ready" and not force:
                return
            self.state = "building"
            self.error = None
            self.progress_bytes = 0
            self._ready.clear()
            self.started_at = time.time()
            self._thread = threading.Thread(target=self._build, name="source-index", daemon=True)
            self._thread.start()

    def _build(self) -> None:
        info = inspect_file(self.path)
        try:
            if not info["readable"]:
                raise FileNotFoundError(info["error"])
            self.total_bytes = info["size"]
            log.info("Building source index for %s (%d bytes)", self.path, info["size"])
            data = build_index(self.path, chunk_size=self.chunk_size,
                               progress=lambda p: setattr(self, "progress_bytes", p))
            after = inspect_file(self.path)
            if after["size"] != info["size"] or after["mtime"] != info["mtime"]:
                raise RuntimeError("The SQL file changed while it was being indexed; rebuild the index.")
            self.store.save_index(str(self.path), info["size"], info["mtime"], data)
            with self._lock:
                self._data = data
                self.state = "ready"
            log.info("Source index ready in %ss: %d tables", data["build_seconds"], len(data["tables"]))
        except SqlParseError as exc:
            log.error("SQL parsing failed while indexing: %s", exc)
            with self._lock:
                self.state, self.error = "error", f"SQL parsing failed: {exc}"
        except Exception as exc:  # noqa: BLE001
            log.exception("Index build failed")
            with self._lock:
                self.state, self.error = "error", str(exc)
        finally:
            self._ready.set()

    def wait_ready(self, cancel: threading.Event | None = None, timeout: float | None = None) -> dict:
        deadline = None if timeout is None else time.time() + timeout
        while not self._ready.wait(0.5):
            if cancel is not None and cancel.is_set():
                raise InterruptedError("Cancelled while waiting for the source index")
            if deadline and time.time() > deadline:
                raise TimeoutError("Timed out waiting for the source index")
        if self.state != "ready" or self._data is None:
            raise RuntimeError(f"Source index unavailable: {self.error or self.state}")
        return self._data

    def get(self) -> dict | None:
        with self._lock:
            return self._data if self.state == "ready" else None

    def table(self, name: str) -> dict | None:
        data = self.get()
        return data["tables"].get(name.lower()) if data else None

    def status(self) -> dict:
        pct = None
        if self.total_bytes:
            pct = round(100.0 * self.progress_bytes / self.total_bytes, 1)
        data = self.get()
        return {
            "state": self.state,
            "error": self.error,
            "progress_bytes": self.progress_bytes,
            "total_bytes": self.total_bytes,
            "percent": pct,
            "elapsed": round(time.time() - self.started_at, 1) if self.started_at and self.state == "building" else None,
            "header": data.get("header") if data else None,
            "statement_counts": data.get("statement_counts") if data else None,
            "notable_statements": (data.get("notable_statements") or [])[:20] if data else None,
            "build_seconds": data.get("build_seconds") if data else None,
        }


def disk_free_bytes(path: str) -> int | None:
    try:
        p = Path(path)
        while not p.exists() and p != p.parent:
            p = p.parent
        import shutil
        return shutil.disk_usage(str(p)).free
    except OSError:
        return None


def is_local_path(path: str) -> bool:
    return bool(path) and os.path.isabs(path)
