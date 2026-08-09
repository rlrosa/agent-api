import os
import pytest
from fastapi.testclient import TestClient
from app.config import get_settings
from app.main import app

os.environ["API_KEY"] = "test-secret-key"


@pytest.fixture(autouse=True)
def use_temp_db(tmp_path):
    test_db = str(tmp_path / "test_api.db")
    os.environ["DB_PATH"] = test_db
    os.environ["API_KEY"] = "test-secret-key"
    os.environ["ALLOW_MOCK_AGENT"] = "1"
    from app.db import init_db
    init_db(test_db)
    yield test_db


def test_healthz():

    with TestClient(app) as client:
        response = client.get("/healthz")
        assert response.status_code == 200
        data = response.json()
        assert data["version"] == "0.1.0"
        assert "queue_depth" in data
        assert "running_count" in data
        assert "effective_concurrency" in data
        assert "agents" in data
        assert "agy" in data["agents"]


def test_auth_header():
    with TestClient(app) as client:
        # Missing key
        r1 = client.get("/v1/jobs")
        assert r1.status_code == 401

        # Invalid key
        r2 = client.get("/v1/jobs", headers={"X-API-Key": "wrong-key"})
        assert r2.status_code == 401

        # Valid key
        r3 = client.get("/v1/jobs", headers={"X-API-Key": "test-secret-key"})
        assert r3.status_code == 200


def test_unknown_agent_returns_400():
    with TestClient(app) as client:
        headers = {"X-API-Key": "test-secret-key"}
        payload = {"agent": "nonexistent_agent", "prompt": "Hello"}
        res = client.post("/v1/jobs", json=payload, headers=headers)
        assert res.status_code == 400
        assert "Unknown agent" in res.json()["detail"]



def test_create_job_wait_zero():
    with TestClient(app) as client:
        headers = {"X-API-Key": "test-secret-key"}
        payload = {"agent": "mock_429", "prompt": "Reply pong", "wait": 0}
        res = client.post("/v1/jobs", json=payload, headers=headers)
        assert res.status_code == 202
        data = res.json()
        assert "job_id" in data
        assert data["status"] == "pending"


def test_get_list_and_delete_job():
    with TestClient(app) as client:
        headers = {"X-API-Key": "test-secret-key"}
        # Create job
        c_res = client.post("/v1/jobs", json={"agent": "mock_429", "prompt": "Test job", "wait": 0}, headers=headers)
        job_id = c_res.json()["job_id"]

        # Get job
        g_res = client.get(f"/v1/jobs/{job_id}", headers=headers)
        assert g_res.status_code == 200
        assert g_res.json()["id"] == job_id

        # List jobs
        l_res = client.get("/v1/jobs", headers=headers)
        assert l_res.status_code == 200
        assert any(j["id"] == job_id for j in l_res.json())

        # Cancel job
        d_res = client.delete(f"/v1/jobs/{job_id}", headers=headers)
        assert d_res.status_code == 200
        assert d_res.json()["status"] == "canceled"

