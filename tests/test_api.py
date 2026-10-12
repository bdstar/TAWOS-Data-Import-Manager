"""HTTP API and dashboard tests (FastAPI TestClient against the disposable test DB)."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.config import EXPECTED_TABLES
from app.main import create_app
from conftest import make_settings


@pytest.fixture()
def client(tmp_path, db_conn):
    app = create_app(make_settings(tmp_path))
    with TestClient(app) as c:
        state = app.state.tawos
        state.index.wait_ready(timeout=60)
        yield c


def wait_job(client, job_id, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] not in ("queued", "running", "cancelling"):
            return job
        time.sleep(0.2)
    raise AssertionError("job did not finish")


def test_dashboard_renders(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "TAWOS Data Import Manager" in r.text


def test_status_lists_all_tables(client):
    s = client.get("/api/status").json()
    assert s["database"]["connected"] is True
    assert "password" not in str(s["database"]).lower()
    assert [t["name"] for t in s["tables"]] == list(EXPECTED_TABLES)
    assert all(t["exists"] for t in s["tables"])
    assert s["source"]["readable"] is True and s["source"]["index"]["state"] == "ready"
    issue = next(t for t in s["tables"] if t["name"] == "issue")
    assert issue["source_records"] == 5 and issue["status_label"] == "Not Started"
    assert s["import_order"][0] == "repository"


def test_unknown_table_rejected(client):
    assert client.post("/api/tables/users;DROP/import").status_code in (404, 405)
    assert client.post("/api/tables/not_a_table/import").status_code == 404
    assert client.get("/api/tables/not_a_table/precheck").status_code == 404


def test_invalid_policy_rejected(client):
    r = client.post("/api/tables/repository/import", json={"duplicate_policy": "truncate"})
    assert r.status_code == 422


def test_dependent_table_blocked_with_explanation(client):
    r = client.post("/api/tables/issue/import", json={})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert any("Import `project` first" in e for e in detail["errors"])


def test_import_via_api_and_counts_refresh(client):
    r = client.post("/api/tables/repository/import", json={"batch_size": 1})
    assert r.status_code == 200, r.text
    job = wait_job(client, r.json()["id"])
    assert job["status"] == "completed" and job["inserted"] == 2 and job["percent"] == 100.0
    s = client.get("/api/status").json()
    repo = next(t for t in s["tables"] if t["name"] == "repository")
    assert repo["existing_records"] == 2
    assert repo["status_label"] == "Completed"
    jobs = client.get("/api/jobs").json()
    assert jobs[0]["id"] == job["id"]
    errs = client.get(f"/api/jobs/{job['id']}/errors").json()
    assert errs["total_stored"] == 0


def test_cancel_finished_job_conflicts(client):
    job = client.post("/api/tables/repository/import", json={}).json()
    wait_job(client, job["id"])
    assert client.post(f"/api/jobs/{job['id']}/cancel").status_code == 409
    assert client.post(f"/api/jobs/{job['id']}/resume").status_code == 409
