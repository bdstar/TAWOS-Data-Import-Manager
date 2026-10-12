"""FastAPI application: dashboard and JSON API."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from . import __version__
from .config import DUPLICATE_POLICIES, ERROR_POLICIES, Settings
from .db import Database
from .importer import ImportOptions, preflight
from .job_manager import JobConflict, JobManager
from .job_store import ACTIVE_STATUSES, JobStore
from .logging_setup import setup_logging
from .source_index import SourceIndexManager, inspect_file

log = logging.getLogger("tawos.web")
HERE = Path(__file__).resolve().parent

STATUS_LABELS = {
    None: "Not Started",
    "queued": "Processing",
    "running": "Processing",
    "cancelling": "Processing",
    "completed": "Completed",
    "completed_with_errors": "Completed with Errors",
    "failed": "Failed",
    "cancelled": "Cancelled",
    "interrupted": "Interrupted",
}


class ImportRequest(BaseModel):
    duplicate_policy: str | None = Field(default=None, description=f"One of {DUPLICATE_POLICIES}")
    error_policy: str | None = Field(default=None, description=f"One of {ERROR_POLICIES}")
    batch_size: int | None = Field(default=None, ge=1, le=100_000)


class ResumeRequest(BaseModel):
    batch_size: int | None = Field(default=None, ge=1, le=100_000)


class AppState:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = JobStore(settings.state_db_path)
        self.db = Database(settings)
        self.index = SourceIndexManager(settings.sql_path, self.store, settings.reader_chunk_bytes)
        self.jobs = JobManager(settings, self.db, self.index, self.store)

    def startup(self) -> None:
        self.jobs.recover()
        if not self.index.load_cached() and self.settings.auto_build_index:
            if inspect_file(self.settings.sql_path)["readable"]:
                self.index.start_build()
        ok, err = self.db.ping()
        if ok:
            try:
                self.db.schema(refresh=True)
                self.db.server_info(refresh=True)
            except Exception as exc:  # noqa: BLE001
                log.warning("Schema inspection failed at startup: %s", exc)
            self.db.refresh_counts_async()
        else:
            log.warning("Database not reachable at startup: %s", err)

    def shutdown(self) -> None:
        self.jobs.shutdown()
        self.db.dispose()
        self.store.close()


def _validate_table(state: AppState, table: str) -> str:
    t = table.lower()
    if t not in state.settings.expected_tables:
        raise HTTPException(404, f"Unknown table {table!r}. Expected one of: {', '.join(state.settings.expected_tables)}")
    return t


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    setup_logging(settings.log_dir)
    state = AppState(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state.startup()
        yield
        state.shutdown()

    app = FastAPI(title="TAWOS Data Import Manager", version=__version__, lifespan=lifespan)
    app.state.tawos = state
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=str(HERE / "templates"))

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        return templates.TemplateResponse(request, "index.html", {
            "version": __version__,
            "duplicate_policies": DUPLICATE_POLICIES,
            "error_policies": ERROR_POLICIES,
            "defaults": {"duplicate_policy": settings.default_duplicate_policy,
                         "error_policy": settings.default_error_policy,
                         "batch_size": settings.batch_size},
        })

    # ------------------------------------------------------------------ status
    @app.get("/api/status")
    def api_status():
        return build_status(state)

    @app.post("/api/counts/refresh")
    def refresh_counts():
        ok, err = state.db.ping()
        if not ok:
            raise HTTPException(503, f"Database unavailable: {err}")
        state.db.schema(refresh=True)
        state.db.refresh_counts_async()
        return {"ok": True}

    @app.post("/api/index/rebuild")
    def rebuild_index():
        if state.jobs.active_tables():
            raise HTTPException(409, "Cannot rebuild the source index while imports are running.")
        state.store.clear_index(str(settings.sql_path))
        state.index.start_build(force=True)
        return state.index.status()

    # ------------------------------------------------------------------ tables
    @app.get("/api/tables/{table}/precheck")
    def precheck(table: str, duplicate_policy: str | None = None):
        t = _validate_table(state, table)
        policy = duplicate_policy or settings.default_duplicate_policy
        if policy not in DUPLICATE_POLICIES:
            raise HTTPException(422, f"duplicate_policy must be one of {DUPLICATE_POLICIES}")
        result = preflight(settings, state.db, state.index, t, policy)
        block = state.jobs.blocking_reason(t)
        if block:
            result["errors"].append(f"[concurrency] {block}")
            result["ok"] = False
        return result

    @app.post("/api/tables/{table}/import")
    def start_import(table: str, req: ImportRequest | None = None):
        t = _validate_table(state, table)
        req = req or ImportRequest()
        opts = ImportOptions(
            duplicate_policy=req.duplicate_policy or settings.default_duplicate_policy,
            error_policy=req.error_policy or settings.default_error_policy,
            batch_size=req.batch_size or settings.batch_size,
            batch_max_bytes=settings.batch_max_bytes,
        )
        try:
            opts.validate()
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        pf = preflight(settings, state.db, state.index, t, opts.duplicate_policy)
        if not pf["ok"]:
            raise HTTPException(409, {"message": "Preflight check failed", "errors": pf["errors"],
                                      "warnings": pf["warnings"]})
        try:
            job = state.jobs.start(t, opts)
        except JobConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        return state.jobs.job_view(job)

    # ------------------------------------------------------------------ jobs
    @app.get("/api/jobs")
    def list_jobs(limit: int = 50, table: str | None = None):
        if table:
            table = _validate_table(state, table)
        limit = max(1, min(limit, 500))
        return [state.jobs.job_view(j) for j in state.store.list_jobs(limit, table)]

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str):
        job = state.store.get_job(job_id)
        if job is None:
            raise HTTPException(404, "Job not found")
        return state.jobs.job_view(job)

    @app.get("/api/jobs/{job_id}/errors")
    def job_errors(job_id: str, limit: int = 200, offset: int = 0):
        if state.store.get_job(job_id) is None:
            raise HTTPException(404, "Job not found")
        errors, total = state.store.get_errors(job_id, max(1, min(limit, 1000)), max(0, offset))
        return {"total_stored": total, "max_stored": settings.max_stored_errors, "errors": errors,
                "full_log": f"logs/jobs/{job_id}_failures.jsonl"}

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str):
        job = state.store.get_job(job_id)
        if job is None:
            raise HTTPException(404, "Job not found")
        if not state.jobs.cancel(job_id):
            raise HTTPException(409, f"Job {job_id} is not running (status: {job['status']}).")
        return {"ok": True, "message": "Cancellation requested; the current batch will finish or roll back."}

    @app.post("/api/jobs/{job_id}/resume")
    def resume_job(job_id: str, req: ResumeRequest | None = None):
        try:
            job = state.jobs.resume(job_id, (req.batch_size if req else None))
        except KeyError as exc:
            raise HTTPException(404, "Job not found") from exc
        except JobConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return state.jobs.job_view(job)

    return app


def build_status(state: AppState) -> dict:
    settings = state.settings
    finfo = inspect_file(settings.sql_path)
    db_ok, db_err = state.db.ping()
    db_info: dict = {"connected": db_ok, "error": db_err, "host": settings.db_host, "port": settings.db_port,
                     "database": settings.db_name, "user": settings.db_user}
    schema = {}
    levels: dict[str, int] = {}
    if db_ok:
        try:
            server = state.db.server_info()
            schema = state.db.schema()
            levels = state.db.dependency_levels()
            db_info.update(server_version=server.version, is_mariadb=server.is_mariadb,
                           max_allowed_packet=server.max_allowed_packet)
        except Exception as exc:  # noqa: BLE001
            db_info["error"] = f"Schema inspection failed: {exc}"
    counts, counts_updated, refreshing = state.db.cached_counts()
    idx = state.index.get()
    latest = state.store.latest_job_per_table()
    active = state.jobs.active_tables()
    order = state.db.import_order(list(settings.expected_tables)) if schema else []

    tables = []
    for name in settings.expected_tables:
        job = latest.get(name)
        view = state.jobs.job_view(job) if job else None
        src = idx["tables"].get(name) if idx else None
        tables.append({
            "name": name,
            "exists": name in schema,
            "existing_records": counts.get(name),
            "counting": name in refreshing,
            "source_records": src["rows"] if src else None,
            "source_bytes": src["bytes"] if src else None,
            "dump_table_name": src["dump_name"] if src else None,
            "dependency_level": levels.get(name),
            "import_order": (order.index(name) + 1) if name in order else None,
            "parents": state.db.parents_of(name) if schema else [],
            "children": state.db.children_of(name) if schema else [],
            "has_primary_key": bool(schema[name].has_real_key) if name in schema else None,
            "status": view["status"] if view else None,
            "status_label": STATUS_LABELS.get(view["status"] if view else None, view["status"] if view else ""),
            "job": view,
            "running": name in active,
            "blocked_reason": state.jobs.blocking_reason(name) if name not in active else None,
        })

    completed = sum(1 for t in tables if t["status"] == "completed")
    with_errors = sum(1 for t in tables if t["status"] == "completed_with_errors")
    failed = sum(1 for t in tables if t["status"] in ("failed", "interrupted", "cancelled"))
    summary = {
        "tables_expected": len(settings.expected_tables),
        "tables_found": sum(1 for t in tables if t["exists"]),
        "completed": completed,
        "completed_with_errors": with_errors,
        "failed_or_stopped": failed,
        "running": len(active),
        "existing_records_total": sum(v for v in (t["existing_records"] for t in tables) if v is not None),
        "source_records_total": sum(v for v in (t["source_records"] for t in tables) if v is not None) if idx else None,
        "counts_updated": counts_updated,
    }
    return {
        "app": {"title": "TAWOS Data Import Manager", "version": __version__},
        "database": db_info,
        "source": {**{k: finfo[k] for k in ("path", "exists", "readable", "size", "error")},
                   "index": state.index.status()},
        "summary": summary,
        "import_order": order,
        "tables": tables,
        "active_statuses": list(ACTIVE_STATUSES),
        "settings": {"batch_size": settings.batch_size, "max_concurrent_jobs": settings.max_concurrent_jobs,
                     "default_duplicate_policy": settings.default_duplicate_policy,
                     "default_error_policy": settings.default_error_policy},
    }


app = None


def get_app() -> FastAPI:
    global app
    if app is None:
        app = create_app()
    return app
