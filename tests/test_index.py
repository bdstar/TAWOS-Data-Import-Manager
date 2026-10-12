from app.job_store import JobStore
from app.source_index import SourceIndexManager, build_index, inspect_file
from conftest import SAMPLE_DUMP, sha256

EXPECTED_ROWS = {
    "affected_version": 3, "change_log": 3, "comment": 3, "component": 2, "fix_version": 3, "issue": 5,
    "issue_component": 3, "issue_link": 2, "project": 2, "repository": 2, "sprint": 1, "user": 3, "version": 2,
}


def test_build_index_counts_and_columns():
    before = sha256(SAMPLE_DUMP)
    data = build_index(SAMPLE_DUMP, chunk_size=4096)
    assert sha256(SAMPLE_DUMP) == before, "the source dump must never be modified"
    assert {k: v["rows"] for k, v in data["tables"].items()} == EXPECTED_ROWS
    issue = data["tables"]["issue"]
    assert issue["dump_name"] == "Issue"
    assert issue["insert_statements"] == 2
    assert len(issue["create_columns"]) == 30
    assert "utf8mb4_0900_ai_ci" in issue["collations"]
    assert issue["data_start"] < issue["data_end"]
    assert data["header"]["source_server_version"] == "8.0.21"


def test_index_cache_roundtrip(tmp_path):
    store = JobStore(tmp_path / "s.sqlite3")
    m = SourceIndexManager(SAMPLE_DUMP, store, 4096)
    assert not m.load_cached()
    m.start_build()
    data = m.wait_ready(timeout=30)
    assert data["tables"]["user"]["rows"] == 3

    m2 = SourceIndexManager(SAMPLE_DUMP, store, 4096)
    assert m2.load_cached(), "a second manager must reuse the cached index"
    assert m2.status()["state"] == "ready"
    store.close()


def test_inspect_missing_file(tmp_path):
    info = inspect_file(tmp_path / "nope.sql")
    assert not info["exists"] and not info["readable"] and "not found" in info["error"]
