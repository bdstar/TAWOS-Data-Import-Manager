"""End-to-end import tests against a disposable MariaDB/MySQL database."""

from __future__ import annotations

import datetime as dt

from app.importer import preflight
from app.job_store import JobStore
from conftest import SAMPLE_DUMP, Components, import_parents, make_settings, sha256

PARENTS_OF_ISSUE = ("repository", "project", "user", "sprint")


def count(conn, table):
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM `{table}`")
        return cur.fetchone()[0]


def all_counts(conn):
    return {t: count(conn, t) for t in ("repository", "project", "user", "sprint", "version", "component",
                                        "issue", "comment", "change_log", "fix_version", "affected_version",
                                        "issue_component", "issue_link")}


def test_import_order_is_derived_from_foreign_keys(components):
    order = components.db.import_order()
    assert order.index("repository") < order.index("project")
    for t in ("user", "sprint", "version", "component"):
        assert order.index("project") < order.index(t)
    for t in ("user", "sprint", "project"):
        assert order.index(t) < order.index("issue")
    for t in ("comment", "change_log", "issue_link", "fix_version", "affected_version", "issue_component"):
        assert order.index("issue") < order.index(t)
    assert set(components.db.parents_of("issue")) == {"project", "user", "sprint"}


def test_prerequisites_block_dependent_table(components):
    pf = preflight(components.settings, components.db, components.index, "issue", "skip_existing")
    assert not pf["ok"]
    joined = " ".join(pf["errors"])
    for parent in ("project", "user", "sprint"):
        assert f"Import `{parent}` first" in joined
    job = components.run_import("issue")
    assert job["status"] == "failed" and "Preflight" in job["error"]


def test_compatibility_warning_for_mysql8_collation(components):
    pf = preflight(components.settings, components.db, components.index, "repository", "skip_existing")
    assert pf["ok"]
    assert any("utf8mb4_0900_ai_ci" in w for w in pf["warnings"])


def test_extra_source_column_blocks_unless_ignored_or_added(tmp_path, db_conn):
    strict = Components(make_settings(tmp_path, ignore_source_columns={}))
    try:
        strict.run_import("repository")
        pf = preflight(strict.settings, strict.db, strict.index, "project", "skip_existing")
        assert not pf["ok"]
        msg = " ".join(pf["errors"])
        assert "SP_Field_ID" in msg and "IGNORE_SOURCE_COLUMNS=project.SP_Field_ID" in msg
        assert "ALTER TABLE `project` ADD COLUMN `SP_Field_ID` varchar(45) DEFAULT NULL AFTER `Last_Update_Date`" in msg

        with db_conn.cursor() as cur:
            cur.execute("ALTER TABLE project ADD COLUMN SP_Field_ID varchar(45) DEFAULT NULL AFTER Last_Update_Date")
        try:
            strict.db.schema(refresh=True)
            job = strict.run_import("project")
            assert job["status"] == "completed", job["error"]
            with db_conn.cursor() as cur:
                cur.execute("SELECT SP_Field_ID, Repository_ID FROM project WHERE ID=1")
                assert cur.fetchone() == ("customfield_10142", 1)
        finally:
            with db_conn.cursor() as cur:
                cur.execute("DELETE FROM project")
                cur.execute("ALTER TABLE project DROP COLUMN SP_Field_ID")
    finally:
        strict.close()


def test_ignored_column_is_reported_as_warning(components, db_conn):
    components.run_import("repository")
    pf = preflight(components.settings, components.db, components.index, "project", "skip_existing")
    assert pf["ok"] and any("excluded by IGNORE_SOURCE_COLUMNS" in w for w in pf["warnings"])
    job = components.run_import("project")
    assert job["status"] == "completed" and job["inserted"] == 2
    with db_conn.cursor() as cur:
        cur.execute("SELECT Repository_ID FROM project ORDER BY ID")
        assert [r[0] for r in cur.fetchall()] == [1, 2]


def test_single_table_import_touches_only_that_table(components, db_conn):
    before_hash = sha256(SAMPLE_DUMP)
    job = components.run_import("repository")
    assert job["status"] == "completed", job["error"]
    assert (job["processed"], job["inserted"], job["skipped"], job["failed"]) == (2, 2, 0, 0)
    counts = all_counts(db_conn)
    assert counts["repository"] == 2
    assert sum(v for k, v in counts.items() if k != "repository") == 0
    assert sha256(SAMPLE_DUMP) == before_hash


def test_full_workflow_text_fidelity_and_fk_failures(components, db_conn):
    import_parents(components, "repository", "project", "user", "sprint", "version", "component")
    job = components.run_import("issue")
    assert job["status"] == "completed_with_errors"
    assert (job["processed"], job["inserted"], job["failed"]) == (5, 4, 1)
    errors, total = components.store.get_errors(job["id"])
    assert total == 1
    assert errors[0]["kind"] == "missing_parent"
    assert "Creator_ID=999" in errors[0]["message"] and "`user`" in errors[0]["message"]
    assert errors[0]["line"] is not None and errors[0]["row_key"] == "ID=104"

    for t in ("comment", "change_log", "fix_version", "affected_version", "issue_component", "issue_link"):
        j = components.run_import(t)
        assert j["status"] == "completed", (t, j["error"])

    assert all_counts(db_conn) == {
        "repository": 2, "project": 2, "user": 3, "sprint": 1, "version": 2, "component": 2, "issue": 4,
        "comment": 3, "change_log": 3, "fix_version": 3, "affected_version": 3, "issue_component": 3,
        "issue_link": 2,
    }
    with db_conn.cursor() as cur:
        cur.execute("SELECT Title, Description, Story_Point, In_Progress_Minutes, Creation_Date FROM issue WHERE ID=100")
        title, desc, sp, minutes, created = cur.fetchone()
        assert title == "Title with 'quote' and \"double\""
        assert desc == "Line1\r\nLine2; path C:\\temp\\new ),( done"
        assert sp == 3.0 and minutes == 120.5 and created == dt.datetime(2013, 5, 1, 10, 0)
        cur.execute("SELECT Title, Description FROM issue WHERE ID=101")
        assert cur.fetchone() == ("It's doubled", "Emoji 🚀 and CJK 日本語 -- not a comment /* nor this */ # nor this")
        cur.execute("SELECT Description, Story_Point, Timespent FROM issue WHERE ID=102")
        assert cur.fetchone() == ("Backslash-escapes: \x00 tab\there \x1a end", -1.5, 1500.0)
        cur.execute("SELECT Description FROM issue WHERE ID=103")
        assert cur.fetchone()[0].replace("\r\n", "\n") == "first line\nsecond line"
        cur.execute("SELECT Description FROM project WHERE ID=1")
        assert cur.fetchone()[0] == "Desc with \\n literal backslash-n and real\nnewline"
        cur.execute("SELECT Description FROM project WHERE ID=2")
        assert cur.fetchone()[0] == "Unicode: émoji 🚀 日本語"
        cur.execute("SELECT Issue_ID, Name, Direction, Target_Issue_ID FROM issue_link WHERE ID=2")
        assert cur.fetchone() == (101, "Blocks", "Inward", 100)


def test_repeat_import_does_not_duplicate(components, db_conn):
    import_parents(components, "repository", "project", "user", "sprint", "version")
    import_parents(components, "issue")
    first = components.run_import("affected_version")
    assert (first["inserted"], first["skipped"]) == (3, 0)
    again = components.run_import("affected_version")
    assert again["status"] == "completed"
    assert (again["inserted"], again["skipped"], again["failed"]) == (0, 3, 0)
    assert count(db_conn, "affected_version") == 3

    again_pk = components.run_import("project")
    assert (again_pk["inserted"], again_pk["skipped"]) == (0, 2)
    assert count(db_conn, "project") == 2


def test_fail_on_duplicate_policy_reports_existing(components, db_conn):
    components.run_import("repository")
    job = components.run_import("repository", duplicate_policy="fail_on_duplicate")
    assert job["status"] == "completed_with_errors"
    assert (job["inserted"], job["failed"]) == (0, 2)
    errors, _ = components.store.get_errors(job["id"])
    assert {e["kind"] for e in errors} == {"duplicate_key"}
    assert count(db_conn, "repository") == 2


def test_upsert_refreshes_existing_rows(components, db_conn):
    components.run_import("repository")
    with db_conn.cursor() as cur:
        cur.execute("UPDATE repository SET Name='changed' WHERE ID=1")
    job = components.run_import("repository", duplicate_policy="upsert")
    assert job["status"] == "completed"
    assert (job["inserted"], job["updated"]) == (0, 2)
    with db_conn.cursor() as cur:
        cur.execute("SELECT Name FROM repository WHERE ID=1")
        assert cur.fetchone()[0] == "Spring"
    assert count(db_conn, "repository") == 2


def test_upsert_rejected_for_keyless_table(components):
    pf = preflight(components.settings, components.db, components.index, "fix_version", "upsert")
    assert any("upsert" in e for e in pf["errors"])


def test_cancel_and_resume_from_checkpoint(components, db_conn):
    import_parents(components, "repository", "project")
    first = components.run_import("user", batch_size=1, cancel_after_batches=1)
    assert first["status"] == "cancelled"
    assert first["inserted"] == 1 and first["checkpoint"]["ordinal"] == 1
    assert count(db_conn, "user") == 1

    resumed = components.run_import("user", batch_size=1, checkpoint_job=first)
    assert resumed["status"] == "completed", resumed["error"]
    assert resumed["resumed_from"] == first["id"]
    assert resumed["skipped"] == 0, "committed rows must not be replayed after resume"
    assert resumed["inserted"] == 2
    assert count(db_conn, "user") == 3


def test_stop_policy_rolls_back_failing_batch(components, db_conn):
    import_parents(components, "repository", "project", "user", "sprint")
    job = components.run_import("issue", error_policy="stop", batch_size=10)
    assert job["status"] == "failed"
    assert "Creator_ID=999" in job["error"] and "rolled back" in job["error"]
    assert count(db_conn, "issue") == 0
    assert job["checkpoint"] is None

    job2 = components.run_import("issue", error_policy="continue", checkpoint_job=job)
    assert (job2["inserted"], job2["failed"]) == (4, 1)


def test_missing_source_file_is_reported(tmp_path, db_conn):
    c = Components(make_settings(tmp_path))
    try:
        broken = make_settings(tmp_path, sql_path=tmp_path / "missing.sql")
        pf = preflight(broken, c.db, c.index, "repository", "skip_existing")
        assert not pf["ok"] and any("File not found" in e for e in pf["errors"])
    finally:
        c.close()


def test_job_history_survives_restart(tmp_path, db_conn):
    settings = make_settings(tmp_path)
    c = Components(settings)
    job = c.run_import("repository")
    running = c.store.create_job("project", duplicate_policy="skip_existing", error_policy="continue",
                                 batch_size=10)
    c.store.update_job(running["id"], status="running")
    c.close()

    store = JobStore(settings.state_db_path)
    assert store.get_job(job["id"])["status"] == "completed"
    assert store.mark_interrupted_jobs() == 1
    assert store.get_job(running["id"])["status"] == "interrupted"
    store.close()
