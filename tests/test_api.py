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


def test_f1_no_default_key_when_api_keys_only(tmp_path):
    from app.security import parse_api_keys
    old_key = os.environ.pop("API_KEY", None)
    os.environ["API_KEYS"] = "alice:key1,bob:key2"
    try:
        keys_map = parse_api_keys()
        assert "default" not in keys_map
        assert keys_map.get("alice") == "key1"
        assert keys_map.get("bob") == "key2"
    finally:
        os.environ.pop("API_KEYS", None)
        if old_key:
            os.environ["API_KEY"] = old_key


def test_f8_multi_tenant_job_isolation():
    os.environ["API_KEYS"] = "alice:key1,bob:key2"
    try:
        with TestClient(app) as client:
            alice_headers = {"X-API-Key": "key1"}
            bob_headers = {"X-API-Key": "key2"}

            # Alice creates a job
            c_res = client.post("/v1/jobs", json={"agent": "mock_429", "prompt": "Alice job", "wait": 0}, headers=alice_headers)
            alice_job_id = c_res.json()["job_id"]

            # Alice can see her job
            g_alice = client.get(f"/v1/jobs/{alice_job_id}", headers=alice_headers)
            assert g_alice.status_code == 200

            # Bob cannot see Alice's job (returns 404)
            g_bob = client.get(f"/v1/jobs/{alice_job_id}", headers=bob_headers)
            assert g_bob.status_code == 404

            # Bob's list_jobs does not include Alice's job
            l_bob = client.get("/v1/jobs", headers=bob_headers)
            assert not any(j["id"] == alice_job_id for j in l_bob.json())

            # Bob cannot cancel Alice's job (returns 404)
            d_bob = client.delete(f"/v1/jobs/{alice_job_id}", headers=bob_headers)
            assert d_bob.status_code == 404

            # Alice can cancel her job
            d_alice = client.delete(f"/v1/jobs/{alice_job_id}", headers=alice_headers)
            assert d_alice.status_code == 200
            assert d_alice.json()["status"] == "canceled"
    finally:
        os.environ.pop("API_KEYS", None)


