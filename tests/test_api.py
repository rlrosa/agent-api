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

            # Bob cannot retry Alice's job (returns 404 - ownership scoping)
            r_bob = client.post(f"/v1/jobs/{alice_job_id}/retry", headers=bob_headers)
            assert r_bob.status_code == 404

            # Alice can cancel her job
            d_alice = client.delete(f"/v1/jobs/{alice_job_id}", headers=alice_headers)
            assert d_alice.status_code == 200
            assert d_alice.json()["status"] == "canceled"

            # Alice can retry her job (resets attempts to 0)
            r_alice = client.post(f"/v1/jobs/{alice_job_id}/retry", headers=alice_headers)
            assert r_alice.status_code == 200
            assert r_alice.json()["status"] == "pending"
            assert r_alice.json()["attempts"] == 0
    finally:
        os.environ.pop("API_KEYS", None)


def test_fuzzy_model_and_effort_resolution():
    from app.agents import build_agy_argv, build_claude_argv, resolve_effort, resolve_model

    # 1. Exact match
    assert resolve_model("claude", "claude-sonnet-5") == "claude-sonnet-5"
    assert resolve_effort("high") == "high"

    # 2. Near-miss resolution
    assert resolve_model("agy", "gemini-3.8-flsh") == "gemini-3.8-flash"
    assert resolve_model("claude", "sonnet") == "claude-sonnet-5"
    assert resolve_effort("med") == "medium"
    assert resolve_effort("HIGH") == "high"

    # 3. Garbage -> Default
    assert resolve_model("agy", "zzzzzz") == "gemini-3.8-flash-low"
    assert resolve_model("claude", "zzzzzz") == "claude-sonnet-5"
    assert resolve_effort("zzzzzz") == "low"

    # 4. Flag-shaped inputs never appear as model or effort values in argv
    agy_argv = build_agy_argv("test", model="--permission-mode", effort="--allowed-tools")
    claude_argv = build_claude_argv("test", model="--permission-mode", effort="--allowed-tools")

    # Deliberate accepted behaviour: --permission-mode lands on premium gemini-4-argon (ratio 0.474), ensuring flag string never reaches argv
    assert agy_argv[agy_argv.index("--model") + 1] == "gemini-4-argon"
    assert agy_argv[agy_argv.index("--effort") + 1] == "low"

    assert claude_argv[claude_argv.index("--model") + 1] == "claude-sonnet-5"
    assert claude_argv[claude_argv.index("--effort") + 1] == "low"

    # 5. Omit flags when unspecified
    claude_default_argv = build_claude_argv("test", model=None, effort=None)
    assert "--model" not in claude_default_argv
    assert "--effort" not in claude_default_argv

    agy_default_argv = build_agy_argv("test", model=None, effort=None)
    assert "--model" in agy_default_argv  # agy model is always emitted
    assert "--effort" not in agy_default_argv  # agy effort is omitted when unspecified


def test_fuzzy_model_cheapest_wins_tiebreak():
    from app.agents import resolve_model

    # Ambiguous inputs that score within 0.10 of a more expensive model resolve downward in cost
    assert resolve_model("agy", "gemini") == "gemini-3.8-flash"  # not gemini-4-argon
    assert resolve_model("agy", "claude") == "claude-sonnet-5-5"  # not claude-opus-5-5
    assert resolve_model("agy", "flash") == "gemini-3.8-flash"
    assert resolve_model("claude", "sonnet") == "claude-sonnet-5"
    assert resolve_model("claude", "haiku") == "claude-haiku-4-5"
    assert resolve_model("claude", "opus-5") == "claude-opus-5"
    assert resolve_model("agy", "gemini-3.8-flsh") == "gemini-3.8-flash"


def test_agy_38_migration_and_argv_resolution():
    from app.agents import build_agy_argv, resolve_model

    # 1. Legacy 3.6 -> 3.8 alias (all four names)
    assert resolve_model("agy", "gemini-3.6-flash") == "gemini-3.8-flash"
    assert resolve_model("agy", "gemini-3.6-flash-low") == "gemini-3.8-flash-low"
    assert resolve_model("agy", "gemini-3.6-flash-medium") == "gemini-3.8-flash-medium"
    assert resolve_model("agy", "gemini-3.6-flash-high") == "gemini-3.8-flash-high"

    # Aliases produce expected argv
    assert build_agy_argv("test", model="gemini-3.6-flash-low")[3:5] == ["--model", "gemini-3.8-flash-low"]
    assert build_agy_argv("test", model="gemini-3.6-flash")[3:5] == ["--model", "gemini-3.8-flash"]
    assert build_agy_argv("test", model="gemini-3.6-flash-medium")[3:5] == ["--model", "gemini-3.8-flash-medium"]
    assert build_agy_argv("test", model="gemini-3.6-flash-high")[3:5] == ["--model", "gemini-3.8-flash-high"]

    # 2. Caller model reaching argv (D2)
    argv = build_agy_argv("test", model="gemini-3.8-flash-high")
    assert argv[argv.index("--model") + 1] == "gemini-3.8-flash-high"
    assert "--effort" not in argv

    # 3. Tier-stripping when effort is supplied (D3)
    argv_stripped = build_agy_argv("test", model="gemini-3.8-flash-low", effort="high")
    assert argv_stripped[argv_stripped.index("--model") + 1] == "gemini-3.8-flash"
    assert argv_stripped[argv_stripped.index("--effort") + 1] == "high"
    assert "gemini-3.8-flash-low" not in argv_stripped


def test_agent_flags_resolution(caplog):
    from app.agents import build_agy_argv, build_claude_argv, get_agent_flags
    from app.config import get_settings

    # 1. Default argv unchanged
    agy_default = build_agy_argv("P", workspace_path="/ws")
    assert agy_default == ['agy', '-p', 'P', '--model', 'gemini-3.8-flash-low', '--sandbox', '--add-dir', '/ws', '--output-format', 'json']

    claude_default = build_claude_argv("P")
    assert claude_default == ['claude', '-p', '--allowed-tools', 'View,Read', '--permission-mode', 'dontAsk', '--output-format', 'json']

    # 2. Operator override replaces spec default
    os.environ["AGENT_FLAGS_CLAUDE"] = "--allowed-tools Read"
    try:
        claude_override = build_claude_argv("P")
        assert claude_override == ['claude', '-p', '--allowed-tools', 'Read', '--output-format', 'json']
    finally:
        os.environ.pop("AGENT_FLAGS_CLAUDE", None)

    # 3. Dropped --sandbox footgun
    os.environ["AGENT_FLAGS_AGY"] = "--dangerously-skip-permissions"
    try:
        agy_override = build_agy_argv("P")
        assert "--sandbox" not in agy_override
        assert "--dangerously-skip-permissions" in agy_override
    finally:
        os.environ.pop("AGENT_FLAGS_AGY", None)

    # 4. Ignored old variable (AGY_SANDBOX_FLAGS) warning
    os.environ["AGY_SANDBOX_FLAGS"] = "--dangerously-skip-permissions"
    try:
        import logging
        with caplog.at_level(logging.WARNING):
            settings = get_settings()
            agy_argv = build_agy_argv("P")
            assert "--sandbox" in agy_argv
            assert "--dangerously-skip-permissions" not in agy_argv
            assert "AGY_SANDBOX_FLAGS is deprecated and ignored" in caplog.text
    finally:
        os.environ.pop("AGY_SANDBOX_FLAGS", None)


def test_stats_models_endpoint():
    with TestClient(app) as client:
        res = client.get("/v1/stats/models", headers={"X-API-Key": "test-secret-key"})
        assert res.status_code == 200
        data = res.json()
        assert isinstance(data, list)
        for item in data:
            assert "agent" in item
            assert "model" in item
            assert "total_jobs" in item
            assert "failed_jobs" in item
            assert "total_tokens" in item


def test_model_prices_config_and_cost_null(tmp_path):
    import json
    from app.config import get_settings
    from app.runner import run_job
    from app.db import create_job, get_job, init_db, finish_job

    db_path = str(tmp_path / "test_cost.db")
    init_db(db_path)

    # 1. Unpriced model -> cost_usd is None
    settings = get_settings()
    assert settings.model_prices == {}
    job = create_job("mock_success", "echo test", model="unpriced-model", db_path=db_path)
    finish_job(job["id"], status="completed", total_tokens=1000, db_path=db_path, cost_usd=None)
    fetched = get_job(job["id"], db_path=db_path)
    assert fetched["cost_usd"] is None

    # 2. Priced model via MODEL_PRICES env var (per-1M tokens)
    # 14,000 in @ $0.30/1M + 200 out @ $2.50/1M = (4200 + 500) / 1M = $0.0047
    os.environ["MODEL_PRICES"] = json.dumps({"gemini-3.8-flash-low": {"input": 0.30, "output": 2.50}})
    try:
        settings_priced = get_settings()
        assert "gemini-3.8-flash-low" in settings_priced.model_prices
        rates = settings_priced.model_prices["gemini-3.8-flash-low"]
        in_t = 14000
        out_t = 200
        computed_cost = round((in_t * rates["input"] + out_t * rates["output"]) / 1_000_000.0, 6)
        assert computed_cost == 0.0047

        job2 = create_job("mock_success", "echo test 2", model="gemini-3.8-flash-low", db_path=db_path)
        finish_job(job2["id"], status="completed", input_tokens=in_t, output_tokens=out_t, total_tokens=in_t+out_t, db_path=db_path, cost_usd=computed_cost)
        fetched2 = get_job(job2["id"], db_path=db_path)
        assert fetched2["cost_usd"] == 0.0047
    finally:
        os.environ.pop("MODEL_PRICES", None)


def test_retry_job_status_guard(tmp_path):
    from app.db import create_job, init_db, finish_job

    with TestClient(app) as client:
        headers = {"X-API-Key": "test-secret-key"}

        # 1. Pending job cannot be retried -> 409 Conflict
        c_res = client.post("/v1/jobs", json={"agent": "mock_429", "prompt": "test pending retry", "wait": 0}, headers=headers)
        assert c_res.status_code == 202
        pending_job_id = c_res.json()["job_id"]
        r_pending = client.post(f"/v1/jobs/{pending_job_id}/retry", headers=headers)
        assert r_pending.status_code == 409
        assert "non-terminal state" in r_pending.json()["detail"]

        # 2. Cancel job -> now terminal, retry succeeds -> 200
        d_res = client.delete(f"/v1/jobs/{pending_job_id}", headers=headers)
        assert d_res.status_code == 200
        r_canceled = client.post(f"/v1/jobs/{pending_job_id}/retry", headers=headers)
        assert r_canceled.status_code == 200
        assert r_canceled.json()["status"] == "pending"
        assert r_canceled.json()["attempts"] == 0


def test_scope_parsing():
    from app.security import parse_api_keys
    old_key = os.environ.pop("API_KEY", None)
    old_keys = os.environ.pop("API_KEYS", None)
    try:
        os.environ["API_KEY"] = "legacy-secret"
        os.environ["API_KEYS"] = (
            "plain:k1,"
            "worker:k2:jobs,"
            "monitor:k3:stats,"
            "admin:k4:jobs+stats+admin,"
            "svc:with:colons:k5:stats,"
            "inv:with:colons:k6:not_a_valid_scope"
        )
        keys_map = parse_api_keys()

        # Legacy default key gets {jobs, stats}
        assert "default" in keys_map
        assert keys_map["default"].secret == "legacy-secret"
        assert keys_map["default"].scopes == {"jobs", "stats"}

        # Plain key gets {jobs, stats}
        assert keys_map["plain"].secret == "k1"
        assert keys_map["plain"].scopes == {"jobs", "stats"}

        # Specific scopes
        assert keys_map["worker"].secret == "k2"
        assert keys_map["worker"].scopes == {"jobs"}

        assert keys_map["monitor"].secret == "k3"
        assert keys_map["monitor"].scopes == {"stats"}

        assert keys_map["admin"].secret == "k4"
        assert keys_map["admin"].scopes == {"jobs", "stats", "admin"}

        # Key with colons where last segment is valid scope
        assert keys_map["svc"].secret == "with:colons:k5"
        assert keys_map["svc"].scopes == {"stats"}

        # Key with colons where last segment is not a valid scope -> treated as part of secret with default scopes
        assert keys_map["inv"].secret == "with:colons:k6:not_a_valid_scope"
        assert keys_map["inv"].scopes == {"jobs", "stats"}

    finally:
        if old_key is not None:
            os.environ["API_KEY"] = old_key
        else:
            os.environ.pop("API_KEY", None)
        if old_keys is not None:
            os.environ["API_KEYS"] = old_keys
        else:
            os.environ.pop("API_KEYS", None)


def test_scope_enforcement_jobs_and_stats():
    old_key = os.environ.pop("API_KEY", None)
    old_keys = os.environ.pop("API_KEYS", None)
    os.environ["API_KEYS"] = "worker:key-jobs:jobs,monitor:key-stats:stats,admin:key-admin:jobs+stats+admin"
    try:
        with TestClient(app) as client:
            jobs_headers = {"X-API-Key": "key-jobs"}
            stats_headers = {"X-API-Key": "key-stats"}

            # Jobs-only key: can create jobs, but 403 on stats and logs
            j_res = client.post("/v1/jobs", json={"agent": "mock_429", "prompt": "hi", "wait": 0}, headers=jobs_headers)
            assert j_res.status_code == 202

            stat_res = client.get("/v1/stats/summary", headers=jobs_headers)
            assert stat_res.status_code == 403
            assert "Missing required scope 'stats'" in stat_res.json()["detail"]

            logs_res = client.get("/v1/logs", headers=jobs_headers)
            assert logs_res.status_code == 403
            assert "Missing required scope 'stats'" in logs_res.json()["detail"]

            # Stats-only key: 403 on creating/getting jobs, 200 on stats
            j_res2 = client.post("/v1/jobs", json={"agent": "mock_429", "prompt": "hi", "wait": 0}, headers=stats_headers)
            assert j_res2.status_code == 403
            assert "Missing required scope 'jobs'" in j_res2.json()["detail"]

            list_res = client.get("/v1/jobs", headers=stats_headers)
            assert list_res.status_code == 403

            stat_res2 = client.get("/v1/stats/summary", headers=stats_headers)
            assert stat_res2.status_code == 200

            logs_res2 = client.get("/v1/logs", headers=stats_headers)
            assert logs_res2.status_code == 200

    finally:
        if old_key is not None:
            os.environ["API_KEY"] = old_key
        else:
            os.environ.pop("API_KEY", None)
        if old_keys is not None:
            os.environ["API_KEYS"] = old_keys
        else:
            os.environ.pop("API_KEYS", None)


def test_v1_me_endpoint():
    old_key = os.environ.pop("API_KEY", None)
    old_keys = os.environ.pop("API_KEYS", None)
    os.environ["API_KEYS"] = "worker:key-jobs:jobs,monitor:key-stats:stats,admin:key-admin:jobs+stats+admin"
    try:
        with TestClient(app) as client:
            # Jobs key
            r_jobs = client.get("/v1/me", headers={"X-API-Key": "key-jobs"})
            assert r_jobs.status_code == 200
            assert r_jobs.json() == {"key_name": "worker", "scopes": ["jobs"]}

            # Stats key
            r_stats = client.get("/v1/me", headers={"X-API-Key": "key-stats"})
            assert r_stats.status_code == 200
            assert r_stats.json() == {"key_name": "monitor", "scopes": ["stats"]}

            # Admin key
            r_admin = client.get("/v1/me", headers={"X-API-Key": "key-admin"})
            assert r_admin.status_code == 200
            assert r_admin.json() == {"key_name": "admin", "scopes": ["admin", "jobs", "stats"]}

            # Trusted bypass (no key, loopback peer)
            with TestClient(app, client=("127.0.0.1", 50000)) as loopback_client:
                r_bypass = loopback_client.get("/v1/me")
                assert r_bypass.status_code == 200
                assert r_bypass.json() == {"key_name": "bypass", "scopes": ["admin", "jobs", "stats"]}

            # Key secret is never exposed
            assert "key-jobs" not in str(r_jobs.json())
            assert "key-admin" not in str(r_admin.json())
    finally:
        if old_key is not None:
            os.environ["API_KEY"] = old_key
        else:
            os.environ.pop("API_KEY", None)
        if old_keys is not None:
            os.environ["API_KEYS"] = old_keys
        else:
            os.environ.pop("API_KEYS", None)


def test_dashboard_gating():
    old_key = os.environ.pop("API_KEY", None)
    old_keys = os.environ.pop("API_KEYS", None)
    os.environ["API_KEYS"] = "worker:key-jobs:jobs,monitor:key-stats:stats"
    try:
        with TestClient(app, client=("127.0.0.1", 50000)) as client:
            # 1. Trusted peer (no CF header, loopback client) -> 200 HTML
            r_trusted = client.get("/dashboard")
            assert r_trusted.status_code == 200
            assert "<!DOCTYPE html>" in r_trusted.text or "<html" in r_trusted.text

            # 2. CF header without key -> 401
            r_cf_no_key = client.get("/dashboard", headers={"cf-ray": "test-ray-id"})
            assert r_cf_no_key.status_code == 401

            # 3. CF header with stats key -> 200 HTML
            r_cf_stats = client.get(
                "/dashboard",
                headers={"cf-ray": "test-ray-id", "X-API-Key": "key-stats"},
            )
            assert r_cf_stats.status_code == 200
            assert "<!DOCTYPE html>" in r_cf_stats.text or "<html" in r_cf_stats.text

            # 4. CF header with jobs-only key -> 401
            r_cf_jobs = client.get(
                "/dashboard",
                headers={"cf-ray": "test-ray-id", "X-API-Key": "key-jobs"},
            )
            assert r_cf_jobs.status_code == 401

    finally:
        if old_key is not None:
            os.environ["API_KEY"] = old_key
        else:
            os.environ.pop("API_KEY", None)
        if old_keys is not None:
            os.environ["API_KEYS"] = old_keys
        else:
            os.environ.pop("API_KEYS", None)


def test_cross_tenant_admin_retry_and_cancel():
    old_key = os.environ.pop("API_KEY", None)
    old_keys = os.environ.pop("API_KEYS", None)
    os.environ["API_KEYS"] = "alice:key-alice:jobs,bob:key-bob:jobs,monitor:key-stats:stats,admin:key-admin:jobs+stats+admin"
    try:
        with TestClient(app) as client:
            # Alice creates job
            c_res = client.post(
                "/v1/jobs",
                json={"agent": "mock_429", "prompt": "alice job", "wait": 0},
                headers={"X-API-Key": "key-alice"},
            )
            assert c_res.status_code == 202
            job_id = c_res.json()["job_id"]

            # Monitor (stats only) tries to cancel -> 403 Forbidden
            mon_res = client.delete(f"/v1/jobs/{job_id}", headers={"X-API-Key": "key-stats"})
            assert mon_res.status_code == 403

            # Bob (jobs scope) tries to cancel Alice's job -> 404 (tenant isolation)
            bob_res = client.delete(f"/v1/jobs/{job_id}", headers={"X-API-Key": "key-bob"})
            assert bob_res.status_code == 404

            # Admin cancels Alice's job -> 200 (cross-tenant allowed)
            admin_cancel = client.delete(f"/v1/jobs/{job_id}", headers={"X-API-Key": "key-admin"})
            assert admin_cancel.status_code == 200
            assert admin_cancel.json()["status"] == "canceled"

            # Admin retries Alice's job -> 200 (cross-tenant retry allowed)
            admin_retry = client.post(f"/v1/jobs/{job_id}/retry", headers={"X-API-Key": "key-admin"})
            assert admin_retry.status_code == 200
            assert admin_retry.json()["status"] == "pending"

    finally:
        if old_key is not None:
            os.environ["API_KEY"] = old_key
        else:
            os.environ.pop("API_KEY", None)
        if old_keys is not None:
            os.environ["API_KEYS"] = old_keys
        else:
            os.environ.pop("API_KEYS", None)


def test_parse_agy_usage_tsv_cases():
    from app.agy_quota import parse_agy_usage_tsv

    # Valid TSV
    valid_tsv = (
        "Gemini Models\tWeekly Limit Remaining\t80%\t2026-11-14T06:29:14Z\n"
        "Gemini Models\tFive Hour Limit Remaining\t92%\t2026-11-09T03:29:14Z\n"
        "Claude Models\tSession Limit\t50%\t2026-11-10T12:00:00Z\n"
    )
    res = parse_agy_usage_tsv(valid_tsv)
    assert res is not None
    assert len(res["buckets"]) == 3
    b1 = res["buckets"][0]
    assert b1["group"] == "Gemini Models"
    assert b1["bucket"] == "Weekly Limit Remaining"
    assert b1["period"] == "weekly"
    assert b1["remaining_percent"] == 80
    assert b1["reset_at"] == "2026-11-14T06:29:14Z"

    b2 = res["buckets"][1]
    assert b2["period"] == "five_hour"
    assert b2["remaining_percent"] == 92

    b3 = res["buckets"][2]
    assert b3["period"] == "other"
    assert b3["remaining_percent"] == 50

    # Malformed lines: fewer than 4 columns
    assert parse_agy_usage_tsv("Gemini\tWeekly\t80%") is None

    # Empty field
    assert parse_agy_usage_tsv("Gemini\tWeekly\t\t2026-11-14T06:29:14Z") is None

    # Bad percent: non-numeric, > 100, < 0, missing %
    assert parse_agy_usage_tsv("Gemini\tWeekly\tabc%\t2026-11-14T06:29:14Z") is None
    assert parse_agy_usage_tsv("Gemini\tWeekly\t150%\t2026-11-14T06:29:14Z") is None
    assert parse_agy_usage_tsv("Gemini\tWeekly\t-5%\t2026-11-14T06:29:14Z") is None
    assert parse_agy_usage_tsv("Gemini\tWeekly\t80\t2026-11-14T06:29:14Z") is None

    # Bad timestamp
    assert parse_agy_usage_tsv("Gemini\tWeekly\t80%\tinvalid-timestamp") is None

    # Empty / non-string
    assert parse_agy_usage_tsv("") is None
    assert parse_agy_usage_tsv("   \n  \n") is None
    assert parse_agy_usage_tsv(None) is None


def test_agy_quota_endpoint_and_caching(tmp_path):
    from app.agy_quota import clear_quota_cache
    from app.db import list_jobs

    fixture_script = os.path.abspath("tests/fixtures/fake_agy_usage.sh")
    counter_file = str(tmp_path / "quota_counter.txt")

    old_cmd = os.environ.get("AGY_USAGE_CMD")
    old_counter = os.environ.get("COUNTER_FILE")
    old_fail = os.environ.get("SIMULATE_FAIL")

    os.environ["AGY_USAGE_CMD"] = fixture_script
    os.environ["COUNTER_FILE"] = counter_file
    os.environ.pop("SIMULATE_FAIL", None)
    clear_quota_cache()

    try:
        with TestClient(app) as client:
            headers = {"X-API-Key": "test-secret-key"}

            # Count rows before
            jobs_before = len(list_jobs(limit=1000))

            # Call 1: runs script, caches result
            r1 = client.get("/v1/stats/agy-quota", headers=headers)
            assert r1.status_code == 200
            d1 = r1.json()
            assert d1["available"] is True
            assert len(d1["buckets"]) == 2
            assert d1["buckets"][0]["group"] == "Gemini Models"
            assert d1["buckets"][0]["period"] == "weekly"

            with open(counter_file, "r") as f:
                c1 = int(f.read().strip())
            assert c1 == 1

            # Call 2: inside TTL -> cached, script not run again
            r2 = client.get("/v1/stats/agy-quota", headers=headers)
            assert r2.status_code == 200
            d2 = r2.json()
            assert d2["available"] is True
            assert d2["fetched_at"] == d1["fetched_at"]

            with open(counter_file, "r") as f:
                c2 = int(f.read().strip())
            assert c2 == 1

            # Assert jobs table row count is unchanged (no job created!)
            jobs_after = len(list_jobs(limit=1000))
            assert jobs_after == jobs_before

            # Test failure path
            os.environ["SIMULATE_FAIL"] = "1"
            clear_quota_cache()

            r_fail = client.get("/v1/stats/agy-quota", headers=headers)
            assert r_fail.status_code == 200
            d_fail = r_fail.json()
            assert d_fail["available"] is False
            assert "error" in d_fail
            assert len(d_fail["buckets"]) == 0

    finally:
        clear_quota_cache()
        if old_cmd is not None:
            os.environ["AGY_USAGE_CMD"] = old_cmd
        else:
            os.environ.pop("AGY_USAGE_CMD", None)
        if old_counter is not None:
            os.environ["COUNTER_FILE"] = old_counter
        else:
            os.environ.pop("COUNTER_FILE", None)
        if old_fail is not None:
            os.environ["SIMULATE_FAIL"] = old_fail
        else:
            os.environ.pop("SIMULATE_FAIL", None)


def test_sources_stats_by_key(tmp_path):
    from app.db import create_job, finish_job

    db_path = os.environ.get("DB_PATH")
    headers = {"X-API-Key": "test-secret-key"}

    # Seed rows with different api_key_name and statuses
    j1 = create_job(agent="mock_429", prompt="p1", auth_mode="key", api_key_name="rondeau-int-wpp", db_path=db_path)
    finish_job(j1["id"], status="completed", total_tokens=100, input_tokens=40, output_tokens=60, db_path=db_path)

    j2 = create_job(agent="mock_429", prompt="p2", auth_mode="key", api_key_name="rondeau-int-wpp", db_path=db_path)
    finish_job(j2["id"], status="failed", error="some error", db_path=db_path)

    j3 = create_job(agent="mock_429", prompt="p3", auth_mode="bypass", api_key_name="bypass", db_path=db_path)
    finish_job(j3["id"], status="completed", total_tokens=250, input_tokens=100, output_tokens=150, db_path=db_path)

    j4 = create_job(agent="mock_429", prompt="p4", auth_mode="key", api_key_name="rondeau-int-admin", db_path=db_path)
    finish_job(j4["id"], status="completed", total_tokens=50, input_tokens=20, output_tokens=30, db_path=db_path)

    with TestClient(app) as client:
        # sources with by=key
        res_by_key = client.get("/v1/stats/sources?by=key", headers=headers)
        assert res_by_key.status_code == 200
        data_key = res_by_key.json()
        assert isinstance(data_key, list)

        key_map = {r["api_key_name"]: r for r in data_key}
        assert "rondeau-int-wpp" in key_map
        assert "bypass" in key_map
        assert "rondeau-int-admin" in key_map

        wpp = key_map["rondeau-int-wpp"]
        assert wpp["total_jobs"] == 2
        assert wpp["completed_jobs"] == 1
        assert wpp["failed_jobs"] == 1
        assert wpp["total_tokens"] == 100
        assert wpp["error_rate"] == 0.5
        assert "last_seen" in wpp

        byp = key_map["bypass"]
        assert byp["total_jobs"] == 1
        assert byp["completed_jobs"] == 1
        assert byp["total_tokens"] == 250

        # default sources without by=key returns list grouped by client_ip, auth_mode
        res_default = client.get("/v1/stats/sources", headers=headers)
        assert res_default.status_code == 200
        data_default = res_default.json()
        assert any("client_ip" in r and "auth_mode" in r for r in data_default)


def test_queue_redaction():
    from app.db import create_job, finish_job

    db_path = os.environ.get("DB_PATH")
    secret_prompt = "TOP_SECRET_PROMPT_DO_NOT_LEAK_XYZ"
    long_error = "E" * 400

    j = create_job(
        agent="mock_429",
        prompt=secret_prompt,
        model="gemini-3.8-flash",
        auth_mode="key",
        api_key_name="test-consumer",
        db_path=db_path,
    )
    finish_job(j["id"], status="failed", error=long_error, stdout="secret stdout", stderr="secret stderr", db_path=db_path)

    headers = {"X-API-Key": "test-secret-key"}
    with TestClient(app) as client:
        res = client.get("/v1/stats/queue", headers=headers)
        assert res.status_code == 200
        raw_text = res.text

        # 1. Prompt text must NEVER appear in queue response
        assert secret_prompt not in raw_text

        # 2. stdout / stderr / metadata must not appear
        assert "secret stdout" not in raw_text
        assert "secret stderr" not in raw_text

        # 3. Find our job
        jobs = res.json()
        target = next((item for item in jobs if item["id"] == j["id"]), None)
        assert target is not None
        assert "prompt" not in target
        assert "stdout" not in target
        assert "stderr" not in target
        assert "metadata" not in target

        # 4. Error is truncated to 300 chars
        assert len(target["error"]) <= 320
        assert target["error"].endswith("... (truncated)")
        assert len(target["error"].replace("... (truncated)", "")) == 300








