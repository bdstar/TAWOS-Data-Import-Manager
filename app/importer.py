"""Table-specific import service.

For one target table the importer:

1. validates the source file, the connection, the column mapping, prerequisite
   (parent) tables and free disk space;
2. streams only that table's INSERT statements from the dump (seeking straight
   to the byte range recorded in the source index);
3. inserts rows in bounded transactions, skipping rows whose key already exists
   and reporting rows whose foreign-key parents are missing;
4. commits a checkpoint after each batch so a failed or cancelled job can be
   resumed without replaying committed work.

Foreign-key and unique checks remain enabled throughout.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import threading
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

import pymysql

from .config import DUPLICATE_POLICIES, ERROR_POLICIES, Settings
from .db import Database, TableSchema, describe_db_error, open_import_connection, quote_ident
from .job_store import JobStore, utcnow
from .source_index import SourceIndexManager, disk_free_bytes, inspect_file
from .sql_parser import (InvalidTextValue, SqlParseError, StatementReader, classify,
                         count_insert_rows, parse_insert)

log = logging.getLogger("tawos.importer")

# MySQL/MariaDB error codes
ER_DUP_ENTRY = 1062
ER_NO_REFERENCED_ROW = (1216, 1452)
ER_LOCK_WAIT_TIMEOUT = 1205
ER_LOCK_DEADLOCK = 1213
ER_NET_PACKET_TOO_LARGE = 1153
CONNECTION_LOST = (2006, 2013, 2003, 2055)
ROW_LEVEL_ERRORS = {1048, 1054, 1062, 1136, 1216, 1264, 1292, 1366, 1406, 1452, 4025, 1265, 1366, 3140}

IN_CHUNK = 1000


class ImportCancelled(Exception):
    pass


class ImportAborted(Exception):
    pass


class PreflightError(Exception):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


@dataclass
class ImportOptions:
    duplicate_policy: str
    error_policy: str
    batch_size: int
    batch_max_bytes: int

    def validate(self) -> None:
        if self.duplicate_policy not in DUPLICATE_POLICIES:
            raise ValueError(f"duplicate_policy must be one of {DUPLICATE_POLICIES}")
        if self.error_policy not in ERROR_POLICIES:
            raise ValueError(f"error_policy must be one of {ERROR_POLICIES}")
        if not 1 <= self.batch_size <= 100_000:
            raise ValueError("batch_size must be between 1 and 100000")


@dataclass
class BatchRow:
    values: tuple
    line: int
    row_in_statement: int
    size: int


# --------------------------------------------------------------------------- #
# Preflight
# --------------------------------------------------------------------------- #

def preflight(settings: Settings, db: Database, index: SourceIndexManager, table: str,
              duplicate_policy: str, *, remaining_bytes: int | None = None,
              require_index: bool = False) -> dict:
    """Validate that `table` can be imported. Returns errors, warnings and info."""
    errors: list[str] = []
    warnings: list[str] = []
    info: dict[str, Any] = {"table": table}

    if table not in settings.expected_tables:
        return {"ok": False, "errors": [f"Table {table!r} is not one of the expected TAWOS tables"],
                "warnings": [], "info": info}

    finfo = inspect_file(settings.sql_path)
    info["source_file"] = {k: finfo[k] for k in ("path", "exists", "readable", "size")}
    if not finfo["readable"]:
        errors.append(f"[source] {finfo['error']}")

    ok, err = db.ping()
    if not ok:
        errors.append(f"[database] Cannot connect to {settings.db_host}:{settings.db_port}/{settings.db_name}: {err}")
        return {"ok": False, "errors": errors, "warnings": warnings, "info": info}

    try:
        server = db.server_info()
        schema = db.schema()
    except Exception as exc:  # noqa: BLE001
        errors.append(f"[database] Schema inspection failed: {describe_db_error(exc)}")
        return {"ok": False, "errors": errors, "warnings": warnings, "info": info}
    info["server_version"] = server.version
    info["max_allowed_packet"] = server.max_allowed_packet

    tschema = schema.get(table)
    if tschema is None:
        errors.append(f"[database] Table `{table}` does not exist in database `{settings.db_name}`")
        return {"ok": False, "errors": errors, "warnings": warnings, "info": info}

    if duplicate_policy == "upsert" and not tschema.has_real_key:
        errors.append(f"[policy] `{table}` has no primary or unique key, so the 'upsert' policy "
                      "cannot be applied. Use 'skip_existing'.")

    # Parent tables (derived from live FK constraints)
    parents = db.parents_of(table)
    info["parents"] = parents
    counts, _, _ = db.cached_counts()
    idx_data = index.get()
    for parent in parents:
        try:
            has = db.has_rows(parent)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"[prerequisite] Could not check `{parent}`: {describe_db_error(exc)}")
            continue
        fk_desc = ", ".join(f"{'/'.join(fk.columns)} -> {parent}.{'/'.join(fk.ref_columns)}"
                            for fk in tschema.foreign_keys if fk.ref_table == parent)
        if not has:
            errors.append(f"[prerequisite] Import `{parent}` first: `{table}` references it ({fk_desc}) "
                          f"and `{parent}` is empty.")
        elif idx_data and parent in idx_data["tables"]:
            src = idx_data["tables"][parent]["rows"]
            existing = counts.get(parent)
            if existing is not None and existing < src:
                warnings.append(f"[prerequisite] `{parent}` has {existing:,} of {src:,} source records; rows "
                                f"referencing missing `{parent}` records will be reported as failed.")

    # Source index dependent checks
    tinfo = index.table(table)
    if tinfo is None:
        if idx_data is None:
            msg = "[source] The source index is not ready yet; the job will wait for it."
            (errors if require_index else warnings).append(msg)
        else:
            errors.append(f"[source] The SQL dump contains no data for table `{table}`.")
    else:
        info["source_rows"] = tinfo["rows"]
        info["source_bytes"] = tinfo["bytes"]
        info["dump_table_name"] = tinfo["dump_name"]
        if tinfo["insert_statements"] == 0:
            errors.append(f"[source] The SQL dump contains no INSERT statements for `{table}`.")
        mapping_errors, mapping_warnings = check_column_mapping(
            tschema, tinfo.get("create_columns"), settings.ignored_columns(table),
            tinfo.get("column_definitions") or {})
        errors.extend(mapping_errors)
        warnings.extend(mapping_warnings)
        unsupported = [c for c in tinfo.get("collations", []) if c not in server.supported_collations]
        if unsupported:
            warnings.append(
                f"[compatibility] The dump's CREATE TABLE for `{tinfo['dump_name']}` uses collation(s) "
                f"{', '.join(unsupported)} which {server.version} does not support. The dump's schema "
                f"definitions are not executed; the existing `{table}` schema is used.")
        key_cols = tschema.key_columns
        src_cols = {c.lower() for c in (tinfo.get("create_columns") or tschema.columns)}
        if duplicate_policy != "upsert" and not all(k.lower() in src_cols for k in key_cols):
            errors.append(f"[integrity] Key columns {key_cols} are not all present in the source data; "
                          "duplicate detection is impossible.")
        if not tschema.has_real_key:
            warnings.append(f"[integrity] `{table}` has no primary/unique key. Duplicates are detected by "
                            f"comparing all columns ({', '.join(tschema.columns)}).")

        # Disk space: data + indexes + undo/redo logs, never just the raw dump size.
        need_src = remaining_bytes if remaining_bytes is not None else tinfo["bytes"]
        required = int(need_src * settings.disk_space_factor + settings.disk_space_reserve_gb * 1024 ** 3)
        free = disk_free_bytes(server.datadir) if server.datadir and Path(server.datadir).exists() else None
        info["disk"] = {"datadir": server.datadir, "free_bytes": free, "estimated_required_bytes": required}
        if free is None:
            warnings.append("[disk] Could not determine free space on the database data directory "
                            "(remote server?). Ensure enough space is available.")
        elif free < required:
            errors.append(f"[disk] Insufficient disk space on the database volume: {free / 1024 ** 3:.1f} GB "
                          f"free, about {required / 1024 ** 3:.1f} GB estimated (data, indexes, logs).")

    return {"ok": not errors, "errors": errors, "warnings": warnings, "info": info}


def check_column_mapping(tschema: TableSchema, dump_columns: list[str] | None,
                         ignored: frozenset = frozenset(),
                         definitions: dict[str, str] | None = None) -> tuple[list[str], list[str]]:
    errors, warnings = [], []
    if not dump_columns:
        warnings.append("[mapping] The dump has no CREATE TABLE for this table; values are mapped to the "
                        "target columns in their database order.")
        return errors, warnings
    live = tschema.column_lookup()
    missing = [c for c in dump_columns if c.lower() not in live]
    skipped = [c for c in missing if c.lower() in ignored]
    blocking = [c for c in missing if c.lower() not in ignored]
    if skipped:
        warnings.append(f"[mapping] Source column(s) {', '.join(skipped)} of `{tschema.name}` are excluded by "
                        "IGNORE_SOURCE_COLUMNS; their values will not be imported.")
    if blocking:
        definitions = definitions or {}
        alters = []
        for c in blocking:
            pos = dump_columns.index(c)
            after = next((dump_columns[i] for i in range(pos - 1, -1, -1) if dump_columns[i].lower() in live), None)
            ddl = definitions.get(c, "<type>")
            alters.append(f"ALTER TABLE `{tschema.name}` ADD COLUMN `{c}` {ddl}"
                          + (f" AFTER `{live[after.lower()]}`" if after else " FIRST") + ";")
        names = ", ".join(f"{tschema.name}.{c}" for c in blocking)
        errors.append(
            f"[mapping] Source column(s) not present in `{tschema.name}`: {', '.join(blocking)}. Importing would "
            f"drop their values. Either add them to the target table ({' '.join(alters)}) or set "
            f"IGNORE_SOURCE_COLUMNS={names} in .env to import without them.")
    extra = [c for c in tschema.columns if c.lower() not in {d.lower() for d in dump_columns}]
    if extra:
        warnings.append(f"[mapping] Target columns without source data (defaults apply): {', '.join(extra)}")
    return errors, warnings


# --------------------------------------------------------------------------- #
# Importer
# --------------------------------------------------------------------------- #

def _norm(v: Any) -> Any:
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (_dt.datetime, _dt.date, _dt.timedelta)):
        return str(v)
    return v


class TableImporter:
    def __init__(self, settings: Settings, db: Database, index: SourceIndexManager, store: JobStore,
                 job: dict, cancel: threading.Event, live: dict,
                 on_finished: Callable[[str], None] | None = None):
        self.settings = settings
        self.db = db
        self.index = index
        self.store = store
        self.job = job
        self.job_id = job["id"]
        self.table = job["table_name"]
        self.cancel = cancel
        self.live = live
        self.on_finished = on_finished
        self.options = ImportOptions(job["duplicate_policy"], job["error_policy"], job["batch_size"],
                                     settings.batch_max_bytes)
        self.counters = {"processed": 0, "inserted": 0, "updated": 0, "skipped": 0, "failed": 0,
                         "batches_committed": 0}
        self.ordinal = 0
        self.errors_stored = 0
        self.conn: pymysql.connections.Connection | None = None
        self.started = time.time()
        self._last_persist = 0.0
        self._fk_cache: dict[str, dict] = {}
        self.failure_log = settings.log_dir / "jobs" / f"{self.job_id}_failures.jsonl"

    # ------------------------------------------------------------------ state
    def _update(self, persist: bool = True, **fields) -> None:
        self.live.update(fields)
        if persist:
            self.store.update_job(self.job_id, **fields)

    def _publish_counters(self, persist: bool, **extra) -> None:
        fields = dict(self.counters, **extra)
        fields["duration_seconds"] = round(time.time() - self.started, 1)
        self.live.update(fields)
        self.live["ordinal"] = self.ordinal
        now = time.time()
        if persist or now - self._last_persist > 2.0:
            self.store.update_job(self.job_id, **fields)
            self._last_persist = now

    def _record_failures(self, failures: list[dict]) -> None:
        if not failures:
            return
        self.failure_log.parent.mkdir(parents=True, exist_ok=True)
        with open(self.failure_log, "a", encoding="utf-8") as fh:
            for f in failures:
                fh.write(json.dumps(f, default=str) + "\n")
        room = self.settings.max_stored_errors - self.errors_stored
        if room > 0:
            self.store.add_errors(self.job_id, failures[:room])
            self.errors_stored += min(room, len(failures))

    def _check_cancel(self) -> None:
        if self.cancel.is_set():
            raise ImportCancelled()

    # ------------------------------------------------------------------ run
    def run(self) -> None:
        status, error = "failed", None
        try:
            self._update(status="running", stage="validating", started_at=utcnow())
            log.info("Job %s: import of `%s` started (policy=%s, errors=%s, batch=%d)", self.job_id,
                     self.table, self.options.duplicate_policy, self.options.error_policy,
                     self.options.batch_size)
            if self.index.get() is None:
                self._update(stage="waiting_for_index")
                self.index.wait_ready(cancel=self.cancel)
            self._check_cancel()

            tinfo = self.index.table(self.table)
            cp = self.job.get("checkpoint")
            remaining = None
            if tinfo and cp:
                remaining = max(0, tinfo["data_end"] - cp["offset"])
            self._update(stage="preflight")
            pf = preflight(self.settings, self.db, self.index, self.table, self.options.duplicate_policy,
                           remaining_bytes=remaining, require_index=True)
            if pf["warnings"]:
                self._update(warnings=pf["warnings"])
            if not pf["ok"]:
                raise PreflightError(pf["errors"])
            tinfo = self.index.table(self.table)
            self._update(total_rows=tinfo["rows"], bytes_start=tinfo["data_start"], bytes_end=tinfo["data_end"])

            self._update(stage="connecting")
            self.conn = open_import_connection(self.settings)
            self._update(stage="importing")
            self._import(tinfo)
            self._update(stage="finalizing")
            try:
                self.db.refresh_counts([self.table])
            except Exception:  # noqa: BLE001
                log.warning("Count refresh failed for %s", self.table)
            status = "completed_with_errors" if self.counters["failed"] else "completed"
        except ImportCancelled:
            status, error = "cancelled", "Cancelled by user. Resume to continue from the last committed batch."
        except PreflightError as exc:
            error = "Preflight check failed: " + " | ".join(exc.errors)
        except ImportAborted as exc:
            error = str(exc)
        except SqlParseError as exc:
            error = f"[parsing] Table `{self.table}`: {exc}"
        except pymysql.err.MySQLError as exc:
            error = f"[database] Table `{self.table}`, stage {self.live.get('stage')}: {describe_db_error(exc)}"
        except InterruptedError as exc:
            status, error = "cancelled", str(exc)
        except OSError as exc:
            error = f"[io] Table `{self.table}`: {exc}"
        except Exception as exc:  # noqa: BLE001
            log.exception("Job %s crashed", self.job_id)
            error = f"[unexpected] Table `{self.table}`, stage {self.live.get('stage')}: {type(exc).__name__}: {exc}"
        finally:
            if self.conn is not None:
                try:
                    self.conn.rollback()
                    self.conn.close()
                except Exception:  # noqa: BLE001
                    pass
            if error:
                log.error("Job %s (%s) %s: %s", self.job_id, self.table, status, error)
            else:
                log.info("Job %s (%s) %s: %s", self.job_id, self.table, status, self.counters)
            self._publish_counters(persist=True)
            self._update(status=status, stage="done", error=error, finished_at=utcnow(),
                         duration_seconds=round(time.time() - self.started, 1))
            if self.on_finished:
                self.on_finished(self.job_id)

    # ------------------------------------------------------------------ main loop
    def _import(self, tinfo: dict) -> None:
        schema = self.db.schema(refresh=True)
        self.tschema = schema[self.table]
        server = self.db.server_info()
        self.max_packet = server.max_allowed_packet
        self.max_stmt = max(1024 * 1024, min(self.max_packet - 1024 * 1024, 16 * 1024 * 1024))

        dump_cols = tinfo.get("create_columns") or list(self.tschema.columns)
        live = self.tschema.column_lookup()
        self.dump_columns = dump_cols
        self.target_columns = [live[c.lower()] for c in dump_cols if c.lower() in live]
        self.key_idx = [self.target_columns.index(k) for k in self.tschema.key_columns
                        if k in self.target_columns]
        self.null_safe_keys = not self.tschema.has_real_key
        self._prepare_sql()

        cp = self.job.get("checkpoint")
        if cp:
            start, start_line, skip_rows, self.ordinal = cp["offset"], cp["line"], cp["row"] or 0, cp["ordinal"] or 0
        else:
            start, start_line, skip_rows, self.ordinal = tinfo["data_start"], tinfo["data_start_line"], 0, 0
        end = tinfo["data_end"]
        self._update(base_ordinal=self.ordinal, bytes_pos=start)

        reader = StatementReader(self.settings.sql_path, start=start, end=end, start_line=start_line,
                                 chunk_size=self.settings.reader_chunk_bytes)
        batch: list[BatchRow] = []
        batch_bytes = 0
        first = True
        for stmt in reader:
            self._check_cancel()
            kind, tname = classify(stmt)
            if kind not in ("insert", "replace") or not tname or tname.lower() != self.table:
                first = False
                continue
            ins = parse_insert(stmt)
            col_map = self._statement_column_map(ins.columns, stmt.line)
            skip = skip_rows if first else 0
            first = False
            row_no = 0
            try:
                for row_no, row in enumerate(ins.iter_rows(), start=1):
                    if row_no <= skip:
                        continue
                    self.ordinal += 1
                    self.counters["processed"] += 1
                    br = self._build_row(row, col_map, stmt.line, row_no)
                    if br is not None:
                        batch.append(br)
                        batch_bytes += br.size
                    if len(batch) >= self.options.batch_size or batch_bytes >= self.options.batch_max_bytes:
                        self._flush(batch, (stmt.offset, stmt.offset_line, row_no, self.ordinal))
                        batch, batch_bytes = [], 0
                    elif self.counters["processed"] % 500 == 0:
                        self._publish_counters(persist=False, bytes_pos=stmt.offset)
            except SqlParseError as exc:
                total = count_insert_rows(stmt)
                lost = max(0, total - row_no)
                self._record_failures([{"stage": "parsing", "kind": "parse_error", "line": exc.line or stmt.line,
                                        "row_in_statement": row_no + 1, "row_key": None,
                                        "message": f"{exc} - {lost} remaining row(s) of this statement skipped"}])
                if self.options.error_policy == "stop":
                    raise
                self.counters["failed"] += lost
                self.counters["processed"] += lost
                self.ordinal += lost
            self._publish_counters(persist=False, bytes_pos=stmt.end_offset)
        if batch or self.counters["batches_committed"] == 0:
            self._flush(batch, (end, None, 0, self.ordinal))
        else:
            self._save_checkpoint((end, None, 0, self.ordinal))

    def _statement_column_map(self, columns: list[str] | None, line: int) -> list[int]:
        """Index of each target column within a source row tuple."""
        src = columns or self.dump_columns
        lookup = {c.lower(): i for i, c in enumerate(src)}
        missing = [c for c in self.target_columns if c.lower() not in lookup]
        if columns and missing:
            raise SqlParseError(f"INSERT column list lacks target columns {missing}", line=line)
        ignored = self.settings.ignored_columns(self.table)
        unknown = [c for c in src if c.lower() not in self.tschema.column_lookup() and c.lower() not in ignored]
        if unknown:
            raise SqlParseError(f"INSERT references columns not in `{self.table}`: {unknown}", line=line)
        self._expected_width = len(src)
        return [lookup[c.lower()] for c in self.target_columns]

    def _build_row(self, row: tuple, col_map: list[int], line: int, row_no: int) -> BatchRow | None:
        if len(row) != self._expected_width:
            self._fail_now(line, row_no, None, "column_count",
                           f"Row has {len(row)} values but {self._expected_width} columns were expected")
            return None
        values = tuple(row[i] for i in col_map)
        size = 0
        for i, v in enumerate(values):
            if isinstance(v, InvalidTextValue):
                self._fail_now(line, row_no, self._key_repr(values), "encoding",
                               f"Column `{self.target_columns[i]}` contains bytes that are not valid UTF-8")
                return None
            if isinstance(v, (str, bytes)):
                size += len(v)
            else:
                size += 8
        if size * 2 + 1024 > self.max_packet:
            exact = sum(len(v.encode("utf-8")) if isinstance(v, str) else (len(v) if isinstance(v, bytes) else 8)
                        for v in values)
            if exact * 2 + 1024 > self.max_packet:
                self._fail_now(line, row_no, self._key_repr(values), "packet_too_large",
                               f"Row is about {exact:,} bytes, which may exceed max_allowed_packet "
                               f"({self.max_packet:,}). Increase max_allowed_packet in my.ini and retry.")
                return None
        return BatchRow(values, line, row_no, size)

    def _fail_now(self, line, row_no, key, kind, message) -> None:
        self.counters["failed"] += 1
        self._record_failures([{"stage": "validation", "kind": kind, "line": line, "row_in_statement": row_no,
                                "row_key": key, "message": message}])
        if self.options.error_policy == "stop":
            raise ImportAborted(f"[validation] `{self.table}` line {line}, row {row_no}: {message}")

    def _key_repr(self, values: tuple) -> str:
        if self.tschema.has_real_key:
            return ", ".join(f"{self.target_columns[i]}={values[i]!r}" for i in self.key_idx)
        return ", ".join(f"{c}={str(values[i])[:40]!r}" for i, c in enumerate(self.target_columns))

    def _prepare_sql(self) -> None:
        tbl = quote_ident(self.tschema.name)
        cols = ", ".join(quote_ident(c) for c in self.target_columns)
        ph = ", ".join(["%s"] * len(self.target_columns))
        self.insert_sql = f"INSERT INTO {tbl} ({cols}) VALUES ({ph})"
        key_set = {self.target_columns[i] for i in self.key_idx}
        updates = [c for c in self.target_columns if c not in key_set]
        if updates:
            self.upsert_sql = self.insert_sql + " ON DUPLICATE KEY UPDATE " + ", ".join(
                f"{quote_ident(c)} = VALUES({quote_ident(c)})" for c in updates)
        else:
            self.upsert_sql = self.insert_sql + f" ON DUPLICATE KEY UPDATE {quote_ident(self.target_columns[0])} = " \
                                                f"{quote_ident(self.target_columns[0])}"

    # ------------------------------------------------------------------ batches
    def _flush(self, batch: list[BatchRow], checkpoint: tuple) -> None:
        self._check_cancel()
        attempts = 0
        while True:
            attempts += 1
            try:
                result = self._process_batch(batch)
                break
            except pymysql.err.OperationalError as exc:
                code = exc.args[0] if exc.args else None
                retryable = code in (ER_LOCK_DEADLOCK, ER_LOCK_WAIT_TIMEOUT) or code in CONNECTION_LOST
                self._safe_rollback()
                if not retryable or attempts >= 3:
                    raise
                log.warning("Job %s: batch retry %d after %s", self.job_id, attempts, describe_db_error(exc))
                time.sleep(2 * attempts)
                if code in CONNECTION_LOST:
                    self._reconnect()
        inserted, updated, skipped, failures = result

        if failures and self.options.error_policy == "stop":
            self._safe_rollback()
            self._record_failures(failures)
            self.counters["failed"] += len(failures)
            first = failures[0]
            raise ImportAborted(
                f"[{first['stage']}] `{self.table}` line {first['line']}, row {first['row_in_statement']}: "
                f"{first['message']}. Error policy is 'stop'; the batch was rolled back. Fix the cause and resume.")
        self.conn.commit()
        self.counters["inserted"] += inserted
        self.counters["updated"] += updated
        self.counters["skipped"] += skipped
        self.counters["failed"] += len(failures)
        self.counters["batches_committed"] += 1
        self._record_failures(failures)
        self._save_checkpoint(checkpoint)
        limit = self.settings.max_failures_before_abort
        if limit and self.counters["failed"] >= limit:
            raise ImportAborted(f"[import] `{self.table}`: {self.counters['failed']:,} failed rows reached the "
                                f"configured limit (MAX_FAILURES_BEFORE_ABORT={limit}). Committed work is kept; "
                                "fix the cause and resume.")

    def _save_checkpoint(self, cp: tuple) -> None:
        offset, line, row, ordinal = cp
        self._publish_counters(persist=True, checkpoint_offset=offset, checkpoint_line=line,
                               checkpoint_row=row, checkpoint_ordinal=ordinal, bytes_pos=offset)

    def _safe_rollback(self) -> None:
        try:
            self.conn.rollback()
        except Exception:  # noqa: BLE001
            pass

    def _reconnect(self) -> None:
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001
            pass
        self.conn = open_import_connection(self.settings)

    def _failure(self, row: BatchRow, stage: str, kind: str, message: str) -> dict:
        return {"stage": stage, "kind": kind, "line": row.line, "row_in_statement": row.row_in_statement,
                "row_key": self._key_repr(row.values), "message": message}

    def _process_batch(self, batch: list[BatchRow]) -> tuple[int, int, int, list[dict]]:
        failures: list[dict] = []
        skipped = 0
        policy = self.options.duplicate_policy
        if not batch:
            return 0, 0, 0, failures

        # 1. duplicates within the batch itself
        seen: set = set()
        unique_rows: list[BatchRow] = []
        for r in batch:
            k = tuple(_norm(r.values[i]) for i in self.key_idx)
            if k in seen:
                if policy == "fail_on_duplicate":
                    failures.append(self._failure(r, "duplicate_check", "duplicate_in_source",
                                                  "Duplicate key within the source data"))
                else:
                    skipped += 1
                continue
            seen.add(k)
            unique_rows.append(r)

        # 2. rows whose key already exists in the target table
        existing = self._existing_keys(unique_rows)
        new_rows, existing_rows = [], []
        for r in unique_rows:
            k = tuple(_norm(r.values[i]) for i in self.key_idx)
            (existing_rows if k in existing else new_rows).append(r)
        if policy == "skip_existing":
            skipped += len(existing_rows)
            existing_rows = []
        elif policy == "fail_on_duplicate":
            failures.extend(self._failure(r, "duplicate_check", "duplicate_key",
                                          "Record already exists in the target table") for r in existing_rows)
            existing_rows = []

        # 3. foreign-key parents
        candidates = new_rows + existing_rows
        fk_failed = self._check_foreign_keys(candidates)
        if fk_failed:
            failures.extend(msg for _, msg in fk_failed.values())
            new_rows = [r for r in new_rows if id(r) not in fk_failed]
            existing_rows = [r for r in existing_rows if id(r) not in fk_failed]

        # 4. insert
        inserted = updated = 0
        if new_rows:
            ok, more_skipped, f = self._insert(new_rows, self.insert_sql)
            inserted += ok
            skipped += more_skipped
            failures.extend(f)
        if existing_rows:
            ok, _, f = self._insert(existing_rows, self.upsert_sql)
            updated += ok
            failures.extend(f)
        return inserted, updated, skipped, failures

    def _existing_keys(self, rows: list[BatchRow]) -> set:
        if not rows or not self.key_idx:
            return set()
        tbl = quote_ident(self.tschema.name)
        cols = [self.target_columns[i] for i in self.key_idx]
        qcols = ", ".join(quote_ident(c) for c in cols)
        found: set = set()
        keys = [tuple(r.values[i] for i in self.key_idx) for r in rows]
        plain = [k for k in keys if None not in k]
        nullish = [k for k in keys if None in k]
        with self.conn.cursor() as cur:
            for i in range(0, len(plain), IN_CHUNK):
                chunk = plain[i:i + IN_CHUNK]
                if len(cols) == 1:
                    sql = f"SELECT {qcols} FROM {tbl} WHERE {qcols} IN ({', '.join(['%s'] * len(chunk))})"
                    cur.execute(sql, [k[0] for k in chunk])
                else:
                    tup = "(" + ", ".join(["%s"] * len(cols)) + ")"
                    sql = f"SELECT {qcols} FROM {tbl} WHERE ({qcols}) IN ({', '.join([tup] * len(chunk))})"
                    cur.execute(sql, [v for k in chunk for v in k])
                found.update(tuple(_norm(v) for v in row) for row in cur.fetchall())
            if nullish and self.null_safe_keys:
                cond = " AND ".join(f"{quote_ident(c)} <=> %s" for c in cols)
                for i in range(0, len(nullish), 200):
                    chunk = nullish[i:i + 200]
                    sql = f"SELECT {qcols} FROM {tbl} WHERE " + " OR ".join([f"({cond})"] * len(chunk))
                    cur.execute(sql, [v for k in chunk for v in k])
                    found.update(tuple(_norm(v) for v in row) for row in cur.fetchall())
        return found

    def _check_foreign_keys(self, rows: list[BatchRow]) -> dict[int, tuple[BatchRow, dict]]:
        """Return rows whose single-column FK values have no parent record."""
        failed: dict[int, tuple[BatchRow, dict]] = {}
        if not rows:
            return failed
        for fk in self.tschema.foreign_keys:
            if len(fk.columns) != 1 or fk.ref_table == self.table:
                continue  # composite or self-referencing keys are enforced by the server
            col = fk.columns[0]
            if col not in self.target_columns:
                continue
            idx = self.target_columns.index(col)
            cache = self._fk_cache.setdefault(fk.name, {})
            values = {r.values[idx] for r in rows if r.values[idx] is not None}
            unknown = [v for v in values if v not in cache]
            if unknown:
                parent = quote_ident(self.db.schema()[fk.ref_table].name)
                rcol = quote_ident(fk.ref_columns[0])
                present: set = set()
                with self.conn.cursor() as cur:
                    for i in range(0, len(unknown), IN_CHUNK):
                        chunk = unknown[i:i + IN_CHUNK]
                        cur.execute(f"SELECT {rcol} FROM {parent} WHERE {rcol} IN ({', '.join(['%s'] * len(chunk))})",
                                    chunk)
                        present.update(_norm(r[0]) for r in cur.fetchall())
                if len(cache) > 500_000:
                    cache.clear()
                for v in unknown:
                    cache[v] = _norm(v) in present
            for r in rows:
                v = r.values[idx]
                if v is not None and not cache.get(v, True) and id(r) not in failed:
                    failed[id(r)] = (r, self._failure(
                        r, "foreign_key_check", "missing_parent",
                        f"{col}={v!r} has no matching record in `{fk.ref_table}`.{fk.ref_columns[0]} "
                        f"(constraint {fk.name})"))
        return failed

    def _insert(self, rows: list[BatchRow], sql: str) -> tuple[int, int, list[dict]]:
        """Bulk insert; on a row-level error fall back to row-by-row in the same transaction."""
        with self.conn.cursor() as cur:
            cur.max_stmt_length = self.max_stmt
            self.conn.query("SAVEPOINT tawos_batch")
            try:
                cur.executemany(sql, [r.values for r in rows])
                return len(rows), 0, []
            except (pymysql.err.IntegrityError, pymysql.err.DataError, pymysql.err.InternalError,
                    pymysql.err.ProgrammingError) as exc:
                log.info("Job %s: bulk insert failed (%s); retrying row by row", self.job_id,
                         describe_db_error(exc))
                self.conn.query("ROLLBACK TO SAVEPOINT tawos_batch")
            except pymysql.err.OperationalError as exc:
                code = exc.args[0] if exc.args else None
                if code in CONNECTION_LOST or code in (ER_LOCK_DEADLOCK, ER_LOCK_WAIT_TIMEOUT):
                    raise
                log.info("Job %s: bulk insert failed (%s); retrying row by row", self.job_id,
                         describe_db_error(exc))
                self.conn.query("ROLLBACK TO SAVEPOINT tawos_batch")

            ok = skipped = 0
            failures: list[dict] = []
            for r in rows:
                try:
                    cur.execute(sql, r.values)
                    ok += 1
                except pymysql.err.OperationalError as exc:
                    code = exc.args[0] if exc.args else None
                    if code in CONNECTION_LOST or code in (ER_LOCK_DEADLOCK, ER_LOCK_WAIT_TIMEOUT):
                        raise
                    failures.append(self._failure(r, "insert", f"db_{code}", describe_db_error(exc)))
                except pymysql.err.MySQLError as exc:
                    code = exc.args[0] if exc.args else None
                    if code == ER_DUP_ENTRY and self.options.duplicate_policy == "skip_existing":
                        skipped += 1
                        continue
                    kind = {ER_DUP_ENTRY: "duplicate_key"}.get(code, "missing_parent" if code in ER_NO_REFERENCED_ROW
                                                                else f"db_{code}")
                    failures.append(self._failure(r, "insert", kind, describe_db_error(exc)))
            return ok, skipped, failures
