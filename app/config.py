"""Application settings loaded from environment variables or a `.env` file."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

EXPECTED_TABLES: tuple[str, ...] = (
    "affected_version",
    "change_log",
    "comment",
    "component",
    "fix_version",
    "issue",
    "issue_component",
    "issue_link",
    "project",
    "repository",
    "sprint",
    "user",
    "version",
)

DUPLICATE_POLICIES = ("skip_existing", "fail_on_duplicate", "upsert")
ERROR_POLICIES = ("continue", "stop")

DEFAULT_SQL_PATH = r"D:\Website\Journal_Research\T2_PM-RAG\TAWOS-dataset\TAWOS.sql\TAWOS.sql"


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


def _env_str(name: str, default: str) -> str:
    raw = os.getenv(name)
    return default if raw is None else raw


def parse_ignore_columns(raw: str) -> dict:
    """Parse "table.column,table.column" into {"table": frozenset({"column"})}."""
    result: dict[str, set] = {}
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        if "." not in item:
            raise ValueError(f"IGNORE_SOURCE_COLUMNS entries must look like table.column, got {item!r}")
        table, column = item.split(".", 1)
        result.setdefault(table.strip().lower(), set()).add(column.strip().lower())
    return {t: frozenset(c) for t, c in result.items()}


def _resolve(path: str) -> Path:
    p = Path(path)
    return p if p.is_absolute() else (PROJECT_ROOT / p).resolve()


@dataclass(frozen=True)
class Settings:
    sql_path: Path = Path(DEFAULT_SQL_PATH)

    db_host: str = "localhost"
    db_port: int = 3306
    db_name: str = "tawos"
    db_user: str = "root"
    db_password: str = field(default="", repr=False)

    batch_size: int = 1000
    batch_max_bytes: int = 8 * 1024 * 1024
    max_concurrent_jobs: int = 2
    default_duplicate_policy: str = "skip_existing"
    default_error_policy: str = "continue"
    max_stored_errors: int = 2000
    max_failures_before_abort: int = 0  # 0 = unlimited
    session_sql_mode: str = "STRICT_TRANS_TABLES,NO_AUTO_VALUE_ON_ZERO,NO_ENGINE_SUBSTITUTION"
    session_time_zone: str = "+00:00"

    disk_space_factor: float = 3.0
    disk_space_reserve_gb: float = 2.0

    reader_chunk_bytes: int = 8 * 1024 * 1024

    state_dir: Path = PROJECT_ROOT / "state"
    log_dir: Path = PROJECT_ROOT / "logs"

    app_host: str = "127.0.0.1"
    app_port: int = 8000
    auto_build_index: bool = True

    expected_tables: tuple[str, ...] = EXPECTED_TABLES
    # Source columns deliberately not imported, as {"table": frozenset({"column", ...})} (lower case).
    ignore_source_columns: dict = field(default_factory=dict)

    def ignored_columns(self, table: str) -> frozenset:
        return self.ignore_source_columns.get(table.lower(), frozenset())

    @property
    def state_db_path(self) -> Path:
        return self.state_dir / "import_state.sqlite3"

    def with_overrides(self, **kwargs) -> "Settings":
        return replace(self, **kwargs)

    @classmethod
    def from_env(cls, env_file: str | os.PathLike | None = None) -> "Settings":
        load_dotenv(env_file or PROJECT_ROOT / ".env", override=False)
        s = cls(
            sql_path=Path(_env_str("TAWOS_SQL_PATH", DEFAULT_SQL_PATH)),
            db_host=_env_str("DB_HOST", "localhost"),
            db_port=_env_int("DB_PORT", 3306),
            db_name=_env_str("DB_NAME", "tawos"),
            db_user=_env_str("DB_USER", "root"),
            db_password=_env_str("DB_PASSWORD", ""),
            batch_size=_env_int("IMPORT_BATCH_SIZE", 1000),
            batch_max_bytes=_env_int("IMPORT_BATCH_MAX_BYTES", 8 * 1024 * 1024),
            max_concurrent_jobs=_env_int("MAX_CONCURRENT_JOBS", 2),
            default_duplicate_policy=_env_str("DEFAULT_DUPLICATE_POLICY", "skip_existing"),
            default_error_policy=_env_str("DEFAULT_ERROR_POLICY", "continue"),
            max_stored_errors=_env_int("MAX_STORED_ERRORS", 2000),
            max_failures_before_abort=_env_int("MAX_FAILURES_BEFORE_ABORT", 0),
            session_sql_mode=_env_str(
                "SESSION_SQL_MODE", "STRICT_TRANS_TABLES,NO_AUTO_VALUE_ON_ZERO,NO_ENGINE_SUBSTITUTION"
            ),
            session_time_zone=_env_str("SESSION_TIME_ZONE", "+00:00"),
            disk_space_factor=_env_float("DISK_SPACE_FACTOR", 3.0),
            disk_space_reserve_gb=_env_float("DISK_SPACE_RESERVE_GB", 2.0),
            reader_chunk_bytes=_env_int("READER_CHUNK_BYTES", 8 * 1024 * 1024),
            state_dir=_resolve(_env_str("STATE_DIR", "state")),
            log_dir=_resolve(_env_str("LOG_DIR", "logs")),
            app_host=_env_str("APP_HOST", "127.0.0.1"),
            app_port=_env_int("APP_PORT", 8000),
            auto_build_index=_env_str("AUTO_BUILD_INDEX", "true").lower() in ("1", "true", "yes"),
            ignore_source_columns=parse_ignore_columns(_env_str("IGNORE_SOURCE_COLUMNS", "")),
        )
        s.validate()
        return s

    def validate(self) -> None:
        if self.default_duplicate_policy not in DUPLICATE_POLICIES:
            raise ValueError(f"DEFAULT_DUPLICATE_POLICY must be one of {DUPLICATE_POLICIES}")
        if self.default_error_policy not in ERROR_POLICIES:
            raise ValueError(f"DEFAULT_ERROR_POLICY must be one of {ERROR_POLICIES}")
        if self.batch_size < 1:
            raise ValueError("IMPORT_BATCH_SIZE must be >= 1")
        if self.max_concurrent_jobs < 1:
            raise ValueError("MAX_CONCURRENT_JOBS must be >= 1")
