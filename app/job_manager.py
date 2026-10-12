"""Background execution of import jobs with safe concurrency rules."""

from __future__ import annotations

import logging
import threading

from .config import Settings
from .db import Database
from .importer import ImportOptions, TableImporter
from .job_store import ACTIVE_STATUSES, RESUMABLE_STATUSES, JobStore
from .source_index import SourceIndexManager

log = logging.getLogger("tawos.jobs")


class JobConflict(Exception):
    pass


class JobManager:
    def __init__(self, settings: Settings, db: Database, index: SourceIndexManager, store: JobStore):
        self.settings = settings
        self.db = db
        self.index = index
        self.store = store
        self._lock = threading.Lock()
        self._threads: dict[str, threading.Thread] = {}
        self._cancel: dict[str, threading.Event] = {}
        self.live: dict[str, dict] = {}
        self._table_job: dict[str, str] = {}

    def recover(self) -> int:
        n = self.store.mark_interrupted_jobs()
        if n:
            log.warning("Marked %d job(s) from a previous run as interrupted (resumable)", n)
        return n

    # ------------------------------------------------------------------ queries
    def active_tables(self) -> dict[str, str]:
        with self._lock:
            return dict(self._table_job)

    def blocking_reason(self, table: str) -> str | None:
        """Why `table` cannot start now, or None. Related tables never run concurrently."""
        with self._lock:
            active = dict(self._table_job)
        if table in active:
            return f"An import of `{table}` is already running (job {active[table]})."
        if len(active) >= self.settings.max_concurrent_jobs:
            return f"The maximum of {self.settings.max_concurrent_jobs} concurrent import job(s) is running."
        try:
            related = set(self.db.parents_of(table)) | set(self.db.children_of(table))
        except Exception:  # noqa: BLE001
            related = set()
        busy = sorted(related & set(active))
        if busy:
            return (f"`{table}` is linked by foreign keys to {', '.join(f'`{b}`' for b in busy)}, which "
                    "is being imported. Wait for that job to finish.")
        return None

    def job_view(self, job: dict) -> dict:
        live = self.live.get(job["id"])
        view = dict(job)
        if live:
            view.update({k: v for k, v in live.items() if k != "warnings" or v})
        view["percent"] = self._percent(view)
        view["error_count"] = self.store.error_count(job["id"]) if view.get("failed") else 0
        view["resumable"] = view["status"] in RESUMABLE_STATUSES
        return view

    @staticmethod
    def _percent(v: dict) -> float | None:
        if v["status"] in ("completed", "completed_with_errors"):
            return 100.0
        total = v.get("total_rows")
        ordinal = v.get("ordinal")
        if ordinal is None:
            ordinal = (v.get("checkpoint_ordinal") or v.get("base_ordinal") or 0)
        if total:
            return round(min(100.0, 100.0 * ordinal / total), 1)
        start, end, pos = v.get("bytes_start"), v.get("bytes_end"), v.get("bytes_pos")
        if start is not None and end and pos is not None and end > start:
            return round(min(100.0, 100.0 * (pos - start) / (end - start)), 1)
        return None

    # ------------------------------------------------------------------ commands
    def start(self, table: str, options: ImportOptions, *, resume_job: dict | None = None) -> dict:
        options.validate()
        if table not in self.settings.expected_tables:
            raise ValueError(f"Unknown table {table!r}")
        reason = self.blocking_reason(table)
        if reason:
            raise JobConflict(reason)
        checkpoint = resume_job.get("checkpoint") if resume_job else None
        job = self.store.create_job(
            table, duplicate_policy=options.duplicate_policy, error_policy=options.error_policy,
            batch_size=options.batch_size, resumed_from=resume_job["id"] if resume_job else None,
            checkpoint=checkpoint, base_ordinal=(checkpoint or {}).get("ordinal") or 0)
        cancel = threading.Event()
        live: dict = {"status": "queued", "stage": "queued"}
        importer = TableImporter(self.settings, self.db, self.index, self.store, job, cancel, live,
                                 on_finished=self._finished)
        thread = threading.Thread(target=importer.run, name=f"import-{table}-{job['id']}", daemon=True)
        with self._lock:
            if table in self._table_job:
                self.store.update_job(job["id"], status="failed", stage="done",
                                      error="Another import of this table started concurrently.")
                raise JobConflict(f"An import of `{table}` is already running.")
            self._table_job[table] = job["id"]
            self._cancel[job["id"]] = cancel
            self._threads[job["id"]] = thread
            self.live[job["id"]] = live
        thread.start()
        log.info("Started job %s for `%s`%s", job["id"], table,
                 f" (resuming {resume_job['id']})" if resume_job else "")
        return self.store.get_job(job["id"])

    def resume(self, job_id: str, batch_size: int | None = None) -> dict:
        old = self.store.get_job(job_id)
        if old is None:
            raise KeyError(job_id)
        if old["status"] not in RESUMABLE_STATUSES:
            raise JobConflict(f"Job {job_id} is {old['status']}; only failed, cancelled or interrupted jobs "
                              "can be resumed.")
        opts = ImportOptions(old["duplicate_policy"], old["error_policy"], batch_size or old["batch_size"],
                             self.settings.batch_max_bytes)
        return self.start(old["table_name"], opts, resume_job=old if old.get("checkpoint") else None)

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            ev = self._cancel.get(job_id)
        if ev is None:
            return False
        ev.set()
        live = self.live.get(job_id)
        if live is not None and live.get("status") in ACTIVE_STATUSES:
            live["status"] = "cancelling"
        return True

    def _finished(self, job_id: str) -> None:
        with self._lock:
            for t, j in list(self._table_job.items()):
                if j == job_id:
                    del self._table_job[t]
            self._cancel.pop(job_id, None)
            self._threads.pop(job_id, None)
        # Keep the final live snapshot briefly; the store holds the durable copy.
        self.live.pop(job_id, None)

    def shutdown(self, timeout: float = 30.0) -> None:
        with self._lock:
            events = list(self._cancel.values())
            threads = list(self._threads.values())
        for ev in events:
            ev.set()
        for t in threads:
            t.join(timeout)
