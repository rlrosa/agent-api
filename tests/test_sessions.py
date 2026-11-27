import os
import time
import pytest
from app.config import Settings, get_settings
from app.agents import (
    AGENTS,
    build_agy_session_argv,
    build_claude_session_argv,
)
from app.db import (
    init_db,
    create_session_row,
    update_session_state,
    get_session,
    list_sessions,
    create_session_turn,
    finish_session_turn,
    list_session_turns,
    close_session_row,
    reset_orphan_sessions,
    purge_old_sessions,
)


@pytest.fixture
def tmp_db_path(tmp_path, monkeypatch):
    db_file = str(tmp_path / "test_sessions.db")
    monkeypatch.setenv("API_KEY", "testkey")
    init_db(db_file)
    return db_file


# ==============================================================================
# 1. Configuration & Dual-Default Parity Tests
# ==============================================================================


def test_session_settings_dual_default_parity(monkeypatch):
    """Verify Settings field defaults exactly match get_settings() fallbacks."""
    for key in [
        "SESSION_WORK_ROOT",
        "SESSION_IDLE_TIMEOUT",
        "SESSION_IDLE_MAX",
        "SESSION_TURN_TIMEOUT",
        "SESSION_MAX_LIFETIME",
        "MAX_ACTIVE_SESSIONS",
        "MAX_QUEUED_TURNS_PER_SESSION",
        "DEFAULT_EFFORT",
    ]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("API_KEY", "dev")

    raw_settings = Settings()
    resolved_settings = get_settings()

    assert raw_settings.session_work_root == "/var/tmp/agent-api/sessions"
    assert resolved_settings.session_work_root == "/var/tmp/agent-api/sessions"

    assert raw_settings.session_idle_timeout == 900
    assert resolved_settings.session_idle_timeout == 900

    assert raw_settings.session_idle_max == 3600
    assert resolved_settings.session_idle_max == 3600

    assert raw_settings.session_turn_timeout == 120
    assert resolved_settings.session_turn_timeout == 120

    assert raw_settings.session_max_lifetime == 86400
    assert resolved_settings.session_max_lifetime == 86400

    assert raw_settings.max_active_sessions == 10
    assert resolved_settings.max_active_sessions == 10

    assert raw_settings.max_queued_turns_per_session == 3
    assert resolved_settings.max_queued_turns_per_session == 3

    assert raw_settings.default_effort is None
    assert resolved_settings.default_effort is None


def test_session_settings_env_overrides(monkeypatch):
    """Verify session settings honour environment variable overrides."""
    monkeypatch.setenv("API_KEY", "dev")
    monkeypatch.setenv("SESSION_WORK_ROOT", "/custom/sessions")
    monkeypatch.setenv("SESSION_IDLE_TIMEOUT", "450")
    monkeypatch.setenv("SESSION_IDLE_MAX", "1800")
    monkeypatch.setenv("SESSION_TURN_TIMEOUT", "60")
    monkeypatch.setenv("SESSION_MAX_LIFETIME", "7200")
    monkeypatch.setenv("MAX_ACTIVE_SESSIONS", "25")
    monkeypatch.setenv("MAX_QUEUED_TURNS_PER_SESSION", "7")
    monkeypatch.setenv("DEFAULT_EFFORT", "med")

    s = get_settings()
    assert s.session_work_root == "/custom/sessions"
    assert s.session_idle_timeout == 450
    assert s.session_idle_max == 1800
    assert s.session_turn_timeout == 60
    assert s.session_max_lifetime == 7200
    assert s.max_active_sessions == 25
    assert s.max_queued_turns_per_session == 7
    assert s.default_effort == "med"


# ==============================================================================
# 2. Stream-JSON Argv Builders Tests
# ==============================================================================


def test_agents_registry_session_argv_builders():
    """Verify session_argv_builder registration in AGENTS dict."""
    assert AGENTS["agy"]["session_argv_builder"] == build_agy_session_argv
    assert AGENTS["claude"]["session_argv_builder"] == build_claude_session_argv
    assert AGENTS["codex"]["session_argv_builder"] is None
    assert AGENTS["mock_429"]["session_argv_builder"] is None


def test_build_agy_session_argv(monkeypatch):
    """Test agy session argv builder rules."""
    monkeypatch.setenv("API_KEY", "dev")
    for k in ("AGY_DEFAULT_MODEL", "CLAUDE_DEFAULT_MODEL", "AGENT_FLAGS_AGY", "AGENT_FLAGS_CLAUDE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SANDBOX_ENABLED", "1")

    # Basic call: --input-format stream-json --output-format stream-json, NO -p
    argv = build_agy_session_argv()
    assert argv[:5] == ["agy", "--input-format", "stream-json", "--output-format", "stream-json"]
    assert "-p" not in argv
    assert "--model" in argv
    assert "gemini-3.8-flash-low" in argv

    # Workspace path: --add-dir /path
    argv_ws = build_agy_session_argv(workspace_path="/tmp/session_1")
    assert "--add-dir" in argv_ws
    assert argv_ws[argv_ws.index("--add-dir") + 1] == "/tmp/session_1"

    # Fuzzy model and effort: 'med' -> 'medium', 'flash' -> 'gemini-3.8-flash'
    argv_eff = build_agy_session_argv(model="flash", effort="med")
    assert "--model" in argv_eff
    # When effort is given, tier suffix must be stripped
    assert argv_eff[argv_eff.index("--model") + 1] == "gemini-3.8-flash"
    assert "--effort" in argv_eff
    assert argv_eff[argv_eff.index("--effort") + 1] == "medium"

    # Tier stripping with explicit tiered model name and effort
    argv_tier = build_agy_session_argv(model="gemini-3.8-flash-high", effort="low")
    assert argv_tier[argv_tier.index("--model") + 1] == "gemini-3.8-flash"
    assert argv_tier[argv_tier.index("--effort") + 1] == "low"

    # AGENT_FLAGS_AGY override
    monkeypatch.setenv("AGENT_FLAGS_AGY", "--sandbox --dangerously-skip-permissions")
    argv_flags = build_agy_session_argv()
    assert "--sandbox" in argv_flags
    assert "--dangerously-skip-permissions" in argv_flags

    # Security: hostile inputs never reach argv (resolved to safe literals)
    argv_hostile = build_agy_session_argv(
        model="--injected-flag; rm -rf /",
        effort="`malicious command`",
    )
    assert "--injected-flag; rm -rf /" not in argv_hostile
    assert "`malicious command`" not in argv_hostile
    # Fallback safe literals
    assert "--effort" in argv_hostile
    assert argv_hostile[argv_hostile.index("--effort") + 1] in ["low", "medium", "high"]


def test_build_claude_session_argv(monkeypatch):
    """Test claude session argv builder rules."""
    monkeypatch.setenv("API_KEY", "dev")
    for k in ("AGY_DEFAULT_MODEL", "CLAUDE_DEFAULT_MODEL", "AGENT_FLAGS_AGY", "AGENT_FLAGS_CLAUDE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SANDBOX_ENABLED", "1")

    # Basic call: claude -p --verbose --input-format stream-json --output-format stream-json
    argv = build_claude_session_argv()
    assert argv[:7] == ["claude", "-p", "--verbose", "--input-format", "stream-json", "--output-format", "stream-json"]
    # Model and effort omitted when unspecified
    assert "--model" not in argv
    assert "--effort" not in argv
    assert "--allowed-tools" in argv

    # When model and effort specified
    argv_spec = build_claude_session_argv(model="claude-haiku-4-5", effort="med")
    assert "--model" in argv_spec
    assert argv_spec[argv_spec.index("--model") + 1] == "claude-haiku-4-5"
    assert "--effort" in argv_spec
    assert argv_spec[argv_spec.index("--effort") + 1] == "medium"

    # AGENT_FLAGS_CLAUDE override
    monkeypatch.setenv("AGENT_FLAGS_CLAUDE", "--permission-mode dontAsk")
    argv_flags = build_claude_session_argv()
    assert "--permission-mode" in argv_flags
    assert "--allowed-tools" not in argv_flags

    # Security: hostile inputs resolved to safe literals
    argv_hostile = build_claude_session_argv(
        model="--arbitrary-injection",
        effort="$(reboot)",
    )
    assert "--arbitrary-injection" not in argv_hostile
    assert "$(reboot)" not in argv_hostile
    assert argv_hostile[argv_hostile.index("--effort") + 1] in ["low", "medium", "high"]


# ==============================================================================
# 3. SQLite Schema & Tenant-Scoped Persistence CRUD Tests
# ==============================================================================


def test_sessions_crud_and_lifecycle(tmp_db_path):
    """Test session creation, updates, and closure."""
    session = create_session_row(
        agent="agy",
        model="gemini-3.8-flash-low",
        effort="low",
        idle_timeout=600,
        turn_timeout=90,
        metadata={"user": "test-user"},
        client_ip="127.0.0.1",
        auth_mode="api_key",
        api_key_name="alice",
        db_path=tmp_db_path,
    )
    assert session is not None
    session_id = session["id"]
    assert session["agent"] == "agy"
    assert session["model"] == "gemini-3.8-flash-low"
    assert session["effort"] == "low"
    assert session["status"] == "starting"
    assert session["turn_count"] == 0
    assert session["idle_timeout"] == 600
    assert session["turn_timeout"] == 90
    assert session["metadata"] == {"user": "test-user"}
    assert session["api_key_name"] == "alice"
    assert session["created_at"] is not None
    assert session["last_active_at"] is not None
    assert session["closed_at"] is None

    # Update session state
    updated = update_session_state(
        session_id,
        status="idle",
        upstream_conversation_id="conv-12345",
        turn_count=1,
        total_attachment_bytes=4096,
        input_tokens=100,
        output_tokens=50,
        cached_tokens=20,
        total_tokens=150,
        cost_usd=0.0005,
        db_path=tmp_db_path,
    )
    assert updated["status"] == "idle"
    assert updated["upstream_conversation_id"] == "conv-12345"
    assert updated["turn_count"] == 1
    assert updated["total_attachment_bytes"] == 4096
    assert updated["input_tokens"] == 100
    assert updated["output_tokens"] == 50
    assert updated["cached_tokens"] == 20
    assert updated["total_tokens"] == 150
    assert updated["cost_usd"] == 0.0005

    # Close session
    closed = close_session_row(session_id, status="closed", db_path=tmp_db_path)
    assert closed["status"] == "closed"
    assert closed["closed_at"] is not None


def test_session_turns_crud(tmp_db_path):
    """Test session turns creation, listing, and finishing."""
    session = create_session_row(agent="agy", db_path=tmp_db_path)
    session_id = session["id"]

    turn1 = create_session_turn(
        session_id=session_id,
        turn_index=0,
        prompt="Turn 1 prompt",
        db_path=tmp_db_path,
    )
    assert turn1["session_id"] == session_id
    assert turn1["turn_index"] == 0
    assert turn1["status"] == "running"
    assert turn1["prompt"] == "Turn 1 prompt"

    turn2 = create_session_turn(
        session_id=session_id,
        turn_index=1,
        prompt="Turn 2 prompt",
        db_path=tmp_db_path,
    )

    # Finish turn 1
    finished1 = finish_session_turn(
        turn_id=turn1["id"],
        status="completed",
        response="Turn 1 response",
        stderr="debug logs",
        input_tokens=50,
        output_tokens=25,
        cached_tokens=10,
        total_tokens=75,
        cost_usd=0.0002,
        db_path=tmp_db_path,
    )
    assert finished1["status"] == "completed"
    assert finished1["response"] == "Turn 1 response"
    assert finished1["stderr"] == "debug logs"
    assert finished1["total_tokens"] == 75
    assert finished1["finished_at"] is not None

    # List turns: ordered by turn_index ASC
    turns = list_session_turns(session_id, db_path=tmp_db_path)
    assert len(turns) == 2
    assert turns[0]["id"] == turn1["id"]
    assert turns[0]["turn_index"] == 0
    assert turns[1]["id"] == turn2["id"]
    assert turns[1]["turn_index"] == 1


def test_session_sql_tenant_scoping(tmp_db_path):
    """Security Invariant #2: Enforce SQL-level tenant isolation between API keys."""
    # Alice creates a session
    s_alice = create_session_row(
        agent="agy",
        api_key_name="alice",
        db_path=tmp_db_path,
    )
    # Bob creates a session
    s_bob = create_session_row(
        agent="claude",
        api_key_name="bob",
        db_path=tmp_db_path,
    )
    # Default unkeyed session
    s_default = create_session_row(
        agent="agy",
        api_key_name=None,
        db_path=tmp_db_path,
    )

    # 1. get_session scoping
    # Alice can fetch her own
    assert get_session(s_alice["id"], db_path=tmp_db_path, api_key_name="alice") is not None
    # Bob CANNOT fetch Alice's session (returns None)
    assert get_session(s_alice["id"], db_path=tmp_db_path, api_key_name="bob") is None
    # Bypass can fetch Alice's session
    assert get_session(s_alice["id"], db_path=tmp_db_path, api_key_name="bypass") is not None
    # Default key sees NULL api_key_name
    assert get_session(s_default["id"], db_path=tmp_db_path, api_key_name="default") is not None
    # Bob cannot see default session
    assert get_session(s_default["id"], db_path=tmp_db_path, api_key_name="bob") is None

    # 2. list_sessions scoping
    alice_list = list_sessions(db_path=tmp_db_path, api_key_name="alice")
    assert len(alice_list) == 1
    assert alice_list[0]["id"] == s_alice["id"]

    bob_list = list_sessions(db_path=tmp_db_path, api_key_name="bob")
    assert len(bob_list) == 1
    assert bob_list[0]["id"] == s_bob["id"]

    bypass_list = list_sessions(db_path=tmp_db_path, api_key_name="bypass")
    assert len(bypass_list) == 3

    # 3. list_session_turns scoping
    create_session_turn(session_id=s_alice["id"], turn_index=0, prompt="Secret", db_path=tmp_db_path)
    # Alice can list her turns
    assert list_session_turns(s_alice["id"], db_path=tmp_db_path, api_key_name="alice") is not None
    # Bob gets None when attempting to list turns on Alice's session
    assert list_session_turns(s_alice["id"], db_path=tmp_db_path, api_key_name="bob") is None

    # 4. close_session_row scoping
    # Bob tries to close Alice's session -> returns None, session remains 'starting'
    bob_close_attempt = close_session_row(
        s_alice["id"],
        status="closed",
        db_path=tmp_db_path,
        api_key_name="bob",
    )
    assert bob_close_attempt is None
    fresh_alice = get_session(s_alice["id"], db_path=tmp_db_path, api_key_name="alice")
    assert fresh_alice["status"] == "starting"

    # Alice can close her own session
    alice_close = close_session_row(
        s_alice["id"],
        status="closed",
        db_path=tmp_db_path,
        api_key_name="alice",
    )
    assert alice_close is not None
    assert alice_close["status"] == "closed"


def test_reset_orphan_sessions(tmp_db_path):
    """Verify reset_orphan_sessions transitions active sessions and running turns on startup."""
    s1 = create_session_row(agent="agy", status="starting", db_path=tmp_db_path)
    s2 = create_session_row(agent="agy", status="idle", db_path=tmp_db_path)
    s3 = create_session_row(agent="claude", status="busy", db_path=tmp_db_path)
    s4 = create_session_row(agent="claude", status="closed", db_path=tmp_db_path)

    t1 = create_session_turn(session_id=s1["id"], turn_index=0, prompt="Running turn", status="running", db_path=tmp_db_path)

    count = reset_orphan_sessions(db_path=tmp_db_path)
    assert count == 3

    # Verify running turn was transitioned to failed
    turns = list_session_turns(s1["id"], db_path=tmp_db_path)
    assert turns[0]["status"] == "failed"
    assert "Interrupted by server restart" in turns[0]["error"]

    for sid in [s1["id"], s2["id"], s3["id"]]:
        s = get_session(sid, db_path=tmp_db_path)
        assert s["status"] == "closed"
        assert s["closed_at"] is not None
        assert "orphaned session" in s["error"]

    # s4 was already closed, error should remain unchanged
    s4_fresh = get_session(s4["id"], db_path=tmp_db_path)
    assert s4_fresh["status"] == "closed"
    assert s4_fresh["error"] is None


def test_purge_old_sessions(tmp_db_path):
    """Verify purge_old_sessions deletes old terminal sessions and their turns."""
    now = time.time()
    old_time = now - 1000.0

    # Old closed session with turn
    s_old = create_session_row(agent="agy", status="closed", db_path=tmp_db_path)
    update_session_state(s_old["id"], closed_at=old_time, db_path=tmp_db_path)
    create_session_turn(session_id=s_old["id"], turn_index=0, prompt="Old prompt", db_path=tmp_db_path)

    # Recent closed session with turn
    s_recent = create_session_row(agent="agy", status="closed", db_path=tmp_db_path)
    update_session_state(s_recent["id"], closed_at=now, db_path=tmp_db_path)
    create_session_turn(session_id=s_recent["id"], turn_index=0, prompt="Recent prompt", db_path=tmp_db_path)

    # Active session (even if old created_at) must NOT be purged
    s_active = create_session_row(agent="agy", status="idle", db_path=tmp_db_path)

    # Purge before now - 500
    purged_count = purge_old_sessions(before_ts=now - 500.0, db_path=tmp_db_path)
    assert purged_count == 1

    # s_old is gone
    assert get_session(s_old["id"], db_path=tmp_db_path) is None
    # s_old turns are also deleted from session_turns table
    assert list_session_turns(s_old["id"], db_path=tmp_db_path) is None
    import sqlite3
    conn = sqlite3.connect(tmp_db_path)
    child_turn_count = conn.execute("SELECT COUNT(*) FROM session_turns WHERE session_id = ?", (s_old["id"],)).fetchone()[0]
    conn.close()
    assert child_turn_count == 0

    # s_recent and s_active survive
    assert get_session(s_recent["id"], db_path=tmp_db_path) is not None
    assert get_session(s_active["id"], db_path=tmp_db_path) is not None


# ==============================================================================
# 4. End-to-End HTTP Integration Tests
# ==============================================================================

from concurrent.futures import ThreadPoolExecutor
from fastapi.testclient import TestClient
from app.main import app
from app.sessions import (
    AgyStreamDriver,
    session_manager,
    sweep_orphaned_session_workspaces,
)
from app.runner import sweep_orphaned_workspaces, wrap_cmd_with_bwrap
from app.security import rate_limiter


@pytest.fixture
def http_client(tmp_path, monkeypatch):
    rate_limiter.history.clear()
    test_db = str(tmp_path / "test_sessions_api.db")
    sess_work_root = str(tmp_path / "sessions_work")
    jobs_work_root = str(tmp_path / "jobs_work")
    os.makedirs(sess_work_root, exist_ok=True)
    os.makedirs(jobs_work_root, exist_ok=True)

    monkeypatch.setenv("DB_PATH", test_db)
    monkeypatch.setenv("SESSION_WORK_ROOT", sess_work_root)
    monkeypatch.setenv("WORK_ROOT", jobs_work_root)
    monkeypatch.setenv("API_KEY", "test-secret-key")
    monkeypatch.setenv("API_KEYS", "alice:key-alice:jobs,bob:key-bob:jobs,monitor:key-monitor:stats,admin:key-admin:jobs+stats+admin")
    monkeypatch.setenv("ALLOW_MOCK_AGENT", "1")
    monkeypatch.setenv("BWRAP_ENABLED", "0")
    monkeypatch.setenv("MODEL_PRICES", '{"gemini-3.8-flash-low": {"input": 0.50, "output": 2.00}}')

    init_db(test_db)
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        yield client


def test_e2e_agy_session_lifecycle(http_client):
    """Test full multi-turn agy stream-json lifecycle, PID stability, deltas, and DELETE."""
    # 1. Turn 1
    r1 = http_client.post(
        "/v1/sessions",
        headers={"X-API-Key": "key-alice"},
        json={
            "agent": "mock_agy_session",
            "model": "gemini-3.8-flash-low",
            "prompt": "Hello Turn 1",
            "idle_timeout": 60,
        },
    )
    assert r1.status_code == 200, r1.text
    d1 = r1.json()
    session_id = d1["id"]
    assert d1["status"] == "idle"
    assert d1["turn_count"] == 1
    assert "Echo [turn 1]: Hello Turn 1" in d1["latest_turn"]["response"]
    assert d1["latest_turn"]["input_tokens"] == 100
    assert d1["latest_turn"]["output_tokens"] == 50
    # Cost: 100 * 0.5/1M + 50 * 2.0/1M = 0.00005 + 0.00010 = 0.00015
    assert d1["latest_turn"]["cost_usd"] == 0.00015
    assert d1["cost_usd"] == 0.00015

    # Check child process PID
    assert session_manager.is_session_active(session_id)
    child_pid = session_manager._sessions[session_id].proc.pid

    # 2. Turn 2
    r2 = http_client.post(
        f"/v1/sessions/{session_id}/messages",
        headers={"X-API-Key": "key-alice"},
        json={"prompt": "Follow-up Turn 2"},
    )
    assert r2.status_code == 200, r2.text
    d2 = r2.json()
    assert d2["turn_count"] == 2
    assert "Echo [turn 2]: Follow-up Turn 2" in d2["latest_turn"]["response"]
    # Per-turn delta: cumulative was 200 input, 100 output -> delta = 100 in, 50 out
    assert d2["latest_turn"]["input_tokens"] == 100
    assert d2["latest_turn"]["output_tokens"] == 50
    assert d2["input_tokens"] == 200
    assert d2["output_tokens"] == 100
    assert d2["cost_usd"] == 0.00030
    # Process continuity: identical PID!
    assert session_manager._sessions[session_id].proc.pid == child_pid

    # 3. Turn 3
    r3 = http_client.post(
        f"/v1/sessions/{session_id}/messages",
        headers={"X-API-Key": "key-alice"},
        json={"prompt": "Turn 3 message"},
    )
    assert r3.status_code == 200, r3.text
    d3 = r3.json()
    assert d3["turn_count"] == 3
    assert session_manager._sessions[session_id].proc.pid == child_pid

    # 4. Inspect turns via GET
    r_get = http_client.get(
        f"/v1/sessions/{session_id}?include_turns=true",
        headers={"X-API-Key": "key-alice"},
    )
    assert r_get.status_code == 200
    d_get = r_get.json()
    assert len(d_get["turns"]) == 3
    assert d_get["turns"][0]["turn_index"] == 0
    assert d_get["turns"][1]["turn_index"] == 1
    assert d_get["turns"][2]["turn_index"] == 2

    # 5. Close session via DELETE
    ws_path = session_manager._sessions[session_id].workspace_path
    r_del = http_client.delete(
        f"/v1/sessions/{session_id}",
        headers={"X-API-Key": "key-alice"},
    )
    assert r_del.status_code == 200
    assert r_del.json()["status"] == "closed"
    # Session no longer active in memory, workspace reaped
    assert not session_manager.is_session_active(session_id)
    assert not os.path.exists(ws_path)


def test_e2e_claude_session_lifecycle(http_client):
    """Test full multi-turn claude stream-json lifecycle and upstream cost delta."""
    r1 = http_client.post(
        "/v1/sessions",
        headers={"X-API-Key": "key-alice"},
        json={
            "agent": "mock_claude_session",
            "prompt": "Hello Claude",
        },
    )
    assert r1.status_code == 200, r1.text
    d1 = r1.json()
    session_id = d1["id"]
    assert d1["turn_count"] == 1
    assert "Echo [turn 1]: Hello Claude" in d1["latest_turn"]["response"]
    assert d1["latest_turn"]["cost_usd"] == 0.0012

    # Turn 2: upstream cost reports 0.0024 -> per-turn cost = 0.0012
    r2 = http_client.post(
        f"/v1/sessions/{session_id}/messages",
        headers={"X-API-Key": "key-alice"},
        json={"prompt": "Turn 2 Claude"},
    )
    assert r2.status_code == 200
    d2 = r2.json()
    assert d2["turn_count"] == 2
    assert d2["latest_turn"]["cost_usd"] == 0.0012
    assert d2["cost_usd"] == 0.0024

    # Close
    r_del = http_client.delete(f"/v1/sessions/{session_id}", headers={"X-API-Key": "key-alice"})
    assert r_del.status_code == 200


def test_spawn_without_initial_prompt_and_wait_zero(http_client):
    """Test session creation without prompt (201 Created) and wait=0 execution (202 Accepted)."""
    # 1. 201 Created when prompt is omitted
    r1 = http_client.post(
        "/v1/sessions",
        headers={"X-API-Key": "key-alice"},
        json={"agent": "mock_agy_session"},
    )
    assert r1.status_code == 201, r1.text
    d1 = r1.json()
    session_id = d1["id"]
    assert d1["status"] == "idle"
    assert d1["turn_count"] == 0
    assert d1.get("expires_at") is not None

    # 2. POST /messages with wait=0 -> 202 Accepted
    r2 = http_client.post(
        f"/v1/sessions/{session_id}/messages",
        headers={"X-API-Key": "key-alice"},
        json={"prompt": "Background turn", "wait": 0},
    )
    assert r2.status_code == 202, r2.text

    # Poll briefly for completion
    completed = False
    for _ in range(50):
        time.sleep(0.05)
        r_chk = http_client.get(f"/v1/sessions/{session_id}", headers={"X-API-Key": "key-alice"})
        if r_chk.status_code == 200 and r_chk.json()["turn_count"] == 1:
            completed = True
            break
    assert completed, "Background turn did not complete in time"

    http_client.delete(f"/v1/sessions/{session_id}", headers={"X-API-Key": "key-alice"})


def _poll_until_expired(http_client, session_id: str, headers: dict, timeout_s: float = 5.0) -> dict:
    deadline = time.time() + timeout_s
    last_resp = None
    while time.time() < deadline:
        r = http_client.get(f"/v1/sessions/{session_id}", headers=headers)
        if r.status_code == 200:
            last_resp = r.json()
            if last_resp.get("status") == "expired" and not session_manager.is_session_active(session_id):
                return last_resp
        time.sleep(0.05)
    raise AssertionError(f"Session {session_id} did not persist expired status within {timeout_s}s; last={last_resp}")


def test_session_idle_auto_expiration(http_client):
    """Test idle reaper auto-expiration, child termination, and 409 Conflict rejection."""
    r1 = http_client.post(
        "/v1/sessions",
        headers={"X-API-Key": "key-alice"},
        json={"agent": "mock_agy_session", "idle_timeout": 5},
    )
    assert r1.status_code == 201
    session_id = r1.json()["id"]

    live = session_manager._sessions[session_id]
    ws_path = live.workspace_path
    assert os.path.exists(ws_path)

    # Fast-forward last_active_at by 10s to trigger reaper immediately
    live.last_active_at = time.time() - 10.0

    expired_doc = _poll_until_expired(http_client, session_id, {"X-API-Key": "key-alice"})
    assert expired_doc["status"] == "expired"
    assert not session_manager.is_session_active(session_id)
    assert not os.path.exists(ws_path)

    # Subsequent POST /messages returns 409 Conflict
    r_msg = http_client.post(
        f"/v1/sessions/{session_id}/messages",
        headers={"X-API-Key": "key-alice"},
        json={"prompt": "Revive?"},
    )
    assert r_msg.status_code == 409
    assert "is expired and cannot accept new messages" in r_msg.json()["detail"]


def test_workspace_isolation_between_jobs_and_sessions(tmp_path, http_client):
    """Verify jobs sweeper does not touch session workspaces, and session sweeper reaps orphans."""
    settings = get_settings()

    # Active session
    r1 = http_client.post(
        "/v1/sessions",
        headers={"X-API-Key": "key-alice"},
        json={"agent": "mock_agy_session"},
    )
    active_id = r1.json()["id"]
    active_ws = session_manager._sessions[active_id].workspace_path

    # Orphaned session folder (closed session)
    orphan_id = "orphan-session-123"
    orphan_ws = os.path.join(settings.session_work_root, orphan_id)
    os.makedirs(orphan_ws, exist_ok=True)
    # Set old mtime
    os.utime(orphan_ws, (time.time() - 400, time.time() - 400))

    # 1. sweep_orphaned_workspaces (for /v1/jobs) MUST NEVER touch SESSION_WORK_ROOT
    sweep_orphaned_workspaces(db_path=settings.db_path)
    assert os.path.exists(active_ws)
    assert os.path.exists(orphan_ws)

    # 2. sweep_orphaned_session_workspaces reaps orphan, preserves active
    reaped = sweep_orphaned_session_workspaces(db_path=settings.db_path, min_age_seconds=300)
    assert reaped == 1
    assert not os.path.exists(orphan_ws)
    assert os.path.exists(active_ws)

    http_client.delete(f"/v1/sessions/{active_id}", headers={"X-API-Key": "key-alice"})


def test_fifo_turn_serialization_and_queue_depth_cap(http_client, monkeypatch, tmp_path):
    """Test controlled FIFO sequential execution overlap via test gate and real queue depth rejection (429)."""
    monkeypatch.setenv("MAX_QUEUED_TURNS_PER_SESSION", "2")
    r1 = http_client.post(
        "/v1/sessions",
        headers={"X-API-Key": "key-alice"},
        json={"agent": "mock_agy_session"},
    )
    session_id = r1.json()["id"]
    live = session_manager._sessions[session_id]
    gate_file = str(tmp_path / "release_turn_zero.gate")

    def send_turn(turn_text):
        return http_client.post(
            f"/v1/sessions/{session_id}/messages",
            headers={"X-API-Key": "key-alice"},
            json={"prompt": turn_text},
        )

    with ThreadPoolExecutor(max_workers=4) as executor:
        # Hold Turn 0 open on a deterministic file gate until after overflow check finishes
        f0 = executor.submit(send_turn, f"WAIT_GATE:{gate_file}:TurnZero")
        try:
            deadline = time.time() + 2.0
            while time.time() < deadline and not live.lock.locked():
                time.sleep(0.01)
            assert live.lock.locked()

            # Submit Turn 1 in known order and wait until queued_waiters == 1
            f1 = executor.submit(send_turn, "QueuedFirst")
            deadline = time.time() + 2.0
            while time.time() < deadline and live.queued_waiters < 1:
                time.sleep(0.01)
            assert live.queued_waiters == 1

            # Submit Turn 2 in known order and wait until queued_waiters == 2 (fills MAX_QUEUED_TURNS_PER_SESSION=2)
            f2 = executor.submit(send_turn, "QueuedSecond")
            deadline = time.time() + 2.0
            while time.time() < deadline and live.queued_waiters < 2:
                time.sleep(0.01)
            assert live.queued_waiters == 2

            # Additional turn while Turn 0 is gated and 2 real turns wait in queue -> HTTP 429 overflow
            r_over = send_turn("RejectedOverflow")
            assert r_over.status_code == 429, r_over.text
            assert "Session turn queue full" in r_over.json()["detail"]
            assert r_over.headers.get("Retry-After") == "5"
        finally:
            with open(gate_file, "w", encoding="utf-8") as gf:
                gf.write("go\n")

        res0 = f0.result()
        res1 = f1.result()
        res2 = f2.result()

    assert res0.status_code == 200
    assert res1.status_code == 200
    assert res2.status_code == 200
    assert f"Echo [turn 1]: WAIT_GATE:{gate_file}:TurnZero" in res0.json()["latest_turn"]["response"]
    assert "Echo [turn 2]: QueuedFirst" in res1.json()["latest_turn"]["response"]
    assert "Echo [turn 3]: QueuedSecond" in res2.json()["latest_turn"]["response"]

    http_client.delete(f"/v1/sessions/{session_id}", headers={"X-API-Key": "key-alice"})


def test_max_active_sessions_cap(http_client, monkeypatch):
    """Test max_active_sessions rejection with HTTP 429."""
    monkeypatch.setenv("MAX_ACTIVE_SESSIONS", "1")
    r1 = http_client.post(
        "/v1/sessions",
        headers={"X-API-Key": "key-alice"},
        json={"agent": "mock_agy_session"},
    )
    assert r1.status_code == 201
    sid1 = r1.json()["id"]

    # Second session creation exceeds limit
    r2 = http_client.post(
        "/v1/sessions",
        headers={"X-API-Key": "key-alice"},
        json={"agent": "mock_agy_session"},
    )
    assert r2.status_code == 429
    assert "Max active sessions limit reached" in r2.json()["detail"]
    assert r2.headers.get("Retry-After") == "10"

    http_client.delete(f"/v1/sessions/{sid1}", headers={"X-API-Key": "key-alice"})


def test_multiturn_attachments_and_cumulative_byte_cap(http_client):
    """Test attachment persistence across turns, cumulative bytes check, and validation guards."""
    import base64
    content1 = b"Invoice #1: $100"
    b64_1 = base64.b64encode(content1).decode("ascii")

    # Turn 1 uploads invoice.txt
    r1 = http_client.post(
        "/v1/sessions",
        headers={"X-API-Key": "key-alice"},
        json={
            "agent": "mock_agy_session",
            "prompt": "CHECK_ATTACHMENT: invoice.txt",
            "attachments": [{"filename": "invoice.txt", "content_b64": b64_1}],
        },
    )
    assert r1.status_code == 200, r1.text
    session_id = r1.json()["id"]
    assert "ATTACHMENT_FOUND: invoice.txt" in r1.json()["latest_turn"]["response"]

    # Turn 2 asks about invoice.txt WITHOUT re-uploading
    r2 = http_client.post(
        f"/v1/sessions/{session_id}/messages",
        headers={"X-API-Key": "key-alice"},
        json={"prompt": "CHECK_ATTACHMENT: invoice.txt"},
    )
    assert r2.status_code == 200
    assert "ATTACHMENT_FOUND: invoice.txt" in r2.json()["latest_turn"]["response"]

    # Output validation guard: agent response containing unreadable pattern fails cleanly
    r_unreadable = http_client.post(
        f"/v1/sessions/{session_id}/messages",
        headers={"X-API-Key": "key-alice"},
        json={"prompt": "UNREADABLE"},
    )
    assert r_unreadable.status_code == 500
    assert "Output validation guard failed" in r_unreadable.json()["detail"]


def test_multi_tenant_http_isolation(http_client):
    """Security Invariant #2: Verify 404 tenant isolation on GET, POST /messages, and DELETE."""
    # Alice creates a session
    r_alice = http_client.post(
        "/v1/sessions",
        headers={"X-API-Key": "key-alice"},
        json={"agent": "mock_agy_session", "prompt": "Alice's secret session"},
    )
    assert r_alice.status_code == 200
    alice_id = r_alice.json()["id"]

    # Bob tries to GET Alice's session -> 404 Not Found (never 403)
    r_bob_get = http_client.get(
        f"/v1/sessions/{alice_id}",
        headers={"X-API-Key": "key-bob"},
    )
    assert r_bob_get.status_code == 404

    # Bob tries to POST message to Alice's session -> 404
    r_bob_msg = http_client.post(
        f"/v1/sessions/{alice_id}/messages",
        headers={"X-API-Key": "key-bob"},
        json={"prompt": "Intrusion attempt"},
    )
    assert r_bob_msg.status_code == 404

    # Bob tries to DELETE Alice's session -> 404
    r_bob_del = http_client.delete(
        f"/v1/sessions/{alice_id}",
        headers={"X-API-Key": "key-bob"},
    )
    assert r_bob_del.status_code == 404

    # Monitor key has 'stats' scope only (no 'jobs' scope) -> 403 Forbidden
    r_mon = http_client.get("/v1/sessions", headers={"X-API-Key": "key-monitor"})
    assert r_mon.status_code == 403

    # Admin key has 'admin' scope -> can GET and DELETE across tenants
    r_admin_get = http_client.get(
        f"/v1/sessions/{alice_id}",
        headers={"X-API-Key": "key-admin"},
    )
    assert r_admin_get.status_code == 200
    assert r_admin_get.json()["id"] == alice_id

    r_admin_del = http_client.delete(
        f"/v1/sessions/{alice_id}",
        headers={"X-API-Key": "key-admin"},
    )
    assert r_admin_del.status_code == 200


def test_turn_timeout_crash_large_output_and_chatty_stderr(http_client):
    """Test turn timeout (504), child crash (500), 256 KiB output, 130 KiB stderr, and OCR 429."""
    # 1. Turn timeout
    r_to = http_client.post(
        "/v1/sessions",
        headers={"X-API-Key": "key-alice"},
        json={"agent": "mock_agy_session", "prompt": "TIMEOUT", "turn_timeout": 1},
    )
    assert r_to.status_code == 504
    assert "timed out" in r_to.json()["detail"]

    # 2. Mid-turn crash
    r_crash = http_client.post(
        "/v1/sessions",
        headers={"X-API-Key": "key-alice"},
        json={"agent": "mock_agy_session", "prompt": "CRASH"},
    )
    assert r_crash.status_code == 500

    # 3. Large output (>64 KiB, specifically 256 KiB) does not raise LimitOverrunError
    r_large = http_client.post(
        "/v1/sessions",
        headers={"X-API-Key": "key-alice"},
        json={"agent": "mock_agy_session", "prompt": "LARGE_OUTPUT"},
    )
    assert r_large.status_code == 200
    assert len(r_large.json()["latest_turn"]["response"]) >= 256 * 1024
    large_id = r_large.json()["id"]
    http_client.delete(f"/v1/sessions/{large_id}", headers={"X-API-Key": "key-alice"})

    # 4. Chatty stderr (>128 KiB, specifically 130 KiB) does not deadlock pipe buffer
    r_stderr = http_client.post(
        "/v1/sessions",
        headers={"X-API-Key": "key-alice"},
        json={"agent": "mock_agy_session", "prompt": "CHATTY_STDERR"},
    )
    assert r_stderr.status_code == 200
    stderr_id = r_stderr.json()["id"]
    http_client.delete(f"/v1/sessions/{stderr_id}", headers={"X-API-Key": "key-alice"})

    # 5. Security Invariant #8: $ 429,00 OCR in response payload is NEVER rate-limited
    r_ocr = http_client.post(
        "/v1/sessions",
        headers={"X-API-Key": "key-alice"},
        json={"agent": "mock_agy_session", "prompt": "OCR_429"},
    )
    assert r_ocr.status_code == 200
    assert "$ 429,00" in r_ocr.json()["latest_turn"]["response"]
    ocr_id = r_ocr.json()["id"]
    http_client.delete(f"/v1/sessions/{ocr_id}", headers={"X-API-Key": "key-alice"})


def test_session_spawn_bwrap_fail_closed(monkeypatch, tmp_path):
    """Test fail-closed confinement error when bwrap is missing and allow_unconfined=0."""
    monkeypatch.setenv("API_KEY", "dev")
    monkeypatch.setenv("BWRAP_ENABLED", "1")
    monkeypatch.setenv("ALLOW_UNCONFINED", "0")
    monkeypatch.setattr("shutil.which", lambda cmd: None if cmd == "bwrap" else "/bin/python3")

    with pytest.raises(Exception) as excinfo:
        wrap_cmd_with_bwrap(["echo", "hi"], str(tmp_path), "mock_agy_session")
    assert "Confinement error: bubblewrap (bwrap) binary missing" in str(excinfo.value)


@pytest.mark.asyncio
async def test_agy_stream_driver_nested_wire_format(monkeypatch):
    """Finding 1: Verify AgyStreamDriver correctly extracts from nested {"event": "result", "result": {...}} wire format."""
    import asyncio
    import json
    monkeypatch.setenv("API_KEY", "dev")
    driver = AgyStreamDriver()
    raw = json.dumps({
        "event": "result",
        "result": {
            "status": "SUCCESS",
            "response": "Hello from nested format",
            "usage": {
                "input_tokens": 120,
                "output_tokens": 45,
                "cached_tokens": 12,
                "total_tokens": 165,
                "cost_usd": 0.00045,
            },
        },
    })
    reader = asyncio.StreamReader()
    reader.feed_data(raw.encode("utf-8") + b"\n")
    reader.feed_eof()

    parsed = await driver.parse_response(
        reader,
        model="gemini-3.8-flash-low",
        settings=get_settings(),
        prev_cum_usage={},
    )
    assert parsed is not None
    assert parsed["is_success"] is True
    assert parsed["response"] == "Hello from nested format"
    assert parsed["turn_usage"]["input_tokens"] == 120
    assert parsed["turn_usage"]["output_tokens"] == 45
    assert parsed["turn_usage"]["cached_tokens"] == 12
    assert parsed["turn_usage"]["total_tokens"] == 165


def test_bypass_tenant_isolation(http_client, monkeypatch):
    """Finding 3: Bypass-created session is not accessible by 'default' API key (404), but accessible by bypass/admin."""
    monkeypatch.setenv("TRUSTED_NETWORKS", "testclient,127.0.0.1")
    # Bypass creation (no API key header from trusted host)
    r_bypass = http_client.post(
        "/v1/sessions",
        json={"agent": "mock_agy_session", "prompt": "Bypass session"},
    )
    assert r_bypass.status_code == 200, r_bypass.text
    session_id = r_bypass.json()["id"]

    # 'default' API key caller tries to GET bypass session -> 404 Not Found
    r_default_get = http_client.get(
        f"/v1/sessions/{session_id}",
        headers={"X-API-Key": "test-secret-key"},
    )
    assert r_default_get.status_code == 404

    # 'default' API key caller tries to POST message to bypass session -> 404 Not Found
    r_default_msg = http_client.post(
        f"/v1/sessions/{session_id}/messages",
        headers={"X-API-Key": "test-secret-key"},
        json={"prompt": "Hijack attempt"},
    )
    assert r_default_msg.status_code == 404

    # 'default' API key caller tries to DELETE bypass session -> 404 Not Found
    r_default_del = http_client.delete(
        f"/v1/sessions/{session_id}",
        headers={"X-API-Key": "test-secret-key"},
    )
    assert r_default_del.status_code == 404

    # Bypass / loopback caller CAN access it
    r_check = http_client.get(f"/v1/sessions/{session_id}")
    assert r_check.status_code == 200
    assert r_check.json()["id"] == session_id

    # Clean up
    http_client.delete(f"/v1/sessions/{session_id}")


def test_multipart_attachments_and_unwedge_on_cap_exceeded(http_client, monkeypatch):
    """Finding 4: Test multipart form-data upload and unwedging of session status if attachment fails."""
    import io
    # 1. Create session via multipart/form-data
    file_content = b"Content from multipart upload 1"
    files = {"file": ("report.txt", io.BytesIO(file_content), "text/plain")}
    data = {"agent": "mock_agy_session", "prompt": "CHECK_ATTACHMENT: report.txt"}
    r1 = http_client.post(
        "/v1/sessions",
        headers={"X-API-Key": "key-alice"},
        data=data,
        files=files,
    )
    assert r1.status_code == 200, r1.text
    session_id = r1.json()["id"]
    assert "ATTACHMENT_FOUND: report.txt" in r1.json()["latest_turn"]["response"]

    # 2. Add message with multipart/form-data
    file2_content = b"Content from multipart upload 2"
    files2 = {"file": ("notes.txt", io.BytesIO(file2_content), "text/plain")}
    data2 = {"prompt": "CHECK_ATTACHMENT: notes.txt"}
    r2 = http_client.post(
        f"/v1/sessions/{session_id}/messages",
        headers={"X-API-Key": "key-alice"},
        data=data2,
        files=files2,
    )
    assert r2.status_code == 200, r2.text
    assert "ATTACHMENT_FOUND: notes.txt" in r2.json()["latest_turn"]["response"]

    # 3. Simulate cumulative byte limit breach on Turn 3
    monkeypatch.setenv("MAX_TOTAL_BYTES", "10")  # very small limit to trigger ValueError
    r3 = http_client.post(
        f"/v1/sessions/{session_id}/messages",
        headers={"X-API-Key": "key-alice"},
        json={
            "prompt": "Exceeding attachment limit",
            "attachments": [{"filename": "huge.bin", "content_b64": "AQIDBAUGBwgJCgsMDQ4PEBES"}],
        },
    )
    assert r3.status_code == 400
    assert "exceeds" in r3.json()["detail"] or "limit" in r3.json()["detail"].lower()

    # Verify session is NOT wedged in 'busy' status
    live = session_manager._sessions[session_id]
    assert live.status == "idle"

    # Turn 4: Follow-up text prompt succeeds because session unwedged
    r4 = http_client.post(
        f"/v1/sessions/{session_id}/messages",
        headers={"X-API-Key": "key-alice"},
        json={"prompt": "Normal recovery turn"},
    )
    assert r4.status_code == 200
    assert "Echo" in r4.json()["latest_turn"]["response"]

    # Clean up
    http_client.delete(f"/v1/sessions/{session_id}", headers={"X-API-Key": "key-alice"})


def test_validation_guards_and_empty_prompt_and_unavailable_agent(http_client):
    """Finding 5: 400 on invalid JSON, 422 on extra params, 400 on empty prompt, 400 on unavailable agent."""
    # 1. Invalid JSON body on POST /v1/sessions -> 400
    r_inv_json = http_client.post(
        "/v1/sessions",
        headers={"X-API-Key": "key-alice", "Content-Type": "application/json"},
        content=b"{not valid json",
    )
    assert r_inv_json.status_code == 400
    assert "Invalid JSON" in r_inv_json.json()["detail"]

    # 2. Extra parameters on POST /v1/sessions -> 422
    r_extra = http_client.post(
        "/v1/sessions",
        headers={"X-API-Key": "key-alice"},
        json={"agent": "mock_agy_session", "unknown_forbidden_param": 123},
    )
    assert r_extra.status_code == 422

    # 3. Unavailable agent -> 400
    r_unavail = http_client.post(
        "/v1/sessions",
        headers={"X-API-Key": "key-alice"},
        json={"agent": "non_existent_agent"},
    )
    assert r_unavail.status_code == 400
    assert "Unknown agent" in r_unavail.json()["detail"]

    # Create a valid session to test /messages validation
    r_sess = http_client.post(
        "/v1/sessions",
        headers={"X-API-Key": "key-alice"},
        json={"agent": "mock_agy_session"},
    )
    assert r_sess.status_code == 201
    sid = r_sess.json()["id"]

    # 4. Invalid JSON on POST /messages -> 400
    r_msg_inv_json = http_client.post(
        f"/v1/sessions/{sid}/messages",
        headers={"X-API-Key": "key-alice", "Content-Type": "application/json"},
        content=b"{bad:json",
    )
    assert r_msg_inv_json.status_code == 400
    assert "Invalid JSON" in r_msg_inv_json.json()["detail"]

    # 5. Empty prompt on POST /messages -> 400
    r_empty_p = http_client.post(
        f"/v1/sessions/{sid}/messages",
        headers={"X-API-Key": "key-alice"},
        json={"prompt": "   "},
    )
    assert r_empty_p.status_code == 400
    assert "prompt" in r_empty_p.json()["detail"].lower()

    # Clean up
    http_client.delete(f"/v1/sessions/{sid}", headers={"X-API-Key": "key-alice"})


def test_per_turn_stderr_isolation(http_client):
    """Finding 6: Stderr from Turn 1 does not leak into Turn 2's latest_turn stderr."""
    # Turn 1: Generates chatty stderr
    r1 = http_client.post(
        "/v1/sessions",
        headers={"X-API-Key": "key-alice"},
        json={"agent": "mock_agy_session", "prompt": "CHATTY_STDERR"},
    )
    assert r1.status_code == 200
    sid = r1.json()["id"]
    turn1_err = r1.json()["latest_turn"]["stderr"]
    assert "E" in turn1_err

    # Turn 2: Normal prompt
    r2 = http_client.post(
        f"/v1/sessions/{sid}/messages",
        headers={"X-API-Key": "key-alice"},
        json={"prompt": "Clean turn 2"},
    )
    assert r2.status_code == 200
    turn2_err = r2.json()["latest_turn"]["stderr"]
    # Turn 2 stderr must NOT contain Turn 1 stderr lines
    assert turn2_err == "" or "E" not in turn2_err

    # Clean up
    http_client.delete(f"/v1/sessions/{sid}", headers={"X-API-Key": "key-alice"})


def test_spawn_only_with_empty_prompt_and_preuploaded_attachments(http_client):
    """Carry-over polish: Empty prompt string or null prompt on creation results in 201 Created spawn-only, and materializes attachments."""
    import io
    content = b"Pre-uploaded file content"
    files = {"file": ("preload.txt", io.BytesIO(content), "text/plain")}
    data = {"agent": "mock_agy_session", "prompt": "   "}
    r1 = http_client.post(
        "/v1/sessions",
        headers={"X-API-Key": "key-alice"},
        data=data,
        files=files,
    )
    assert r1.status_code == 201, r1.text
    sid = r1.json()["id"]
    assert r1.json()["status"] == "idle"
    assert r1.json()["turn_count"] == 0
    assert r1.json()["total_attachment_bytes"] == len(content)

    # Turn 1 asks about the preloaded attachment
    r2 = http_client.post(
        f"/v1/sessions/{sid}/messages",
        headers={"X-API-Key": "key-alice"},
        json={"prompt": "CHECK_ATTACHMENT: preload.txt"},
    )
    assert r2.status_code == 200
    assert "ATTACHMENT_FOUND: preload.txt" in r2.json()["latest_turn"]["response"]

    http_client.delete(f"/v1/sessions/{sid}", headers={"X-API-Key": "key-alice"})


def test_helper_utils_session_cli_and_dev_server_guard(tmp_path):
    """Verify scripts/session-cli 0600 cache handling & default URL, and scripts/dev-server.sh port 8090 guard."""
    import json
    import stat
    import subprocess
    import sys

    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    cli_path = os.path.join(repo_root, "scripts", "session-cli")
    dev_srv_path = os.path.join(repo_root, "scripts", "dev-server.sh")

    state_file = str(tmp_path / "cli-state.json")
    clean_env = {k: v for k, v in os.environ.items() if k not in ("AGENT_API_URL", "AGENT_API_KEY")}

    # 1. Saving config writes 0600 file and preserves default :8090 URL when --url is omitted
    res_save = subprocess.run(
        [sys.executable, cli_path, "--state-file", state_file, "--effort", "med", "--save-config"],
        env=clean_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_save.returncode == 0, res_save.stderr
    mode = stat.S_IMODE(os.stat(state_file).st_mode)
    assert mode == 0o600
    with open(state_file, "r", encoding="utf-8") as f:
        saved = json.load(f)
    assert saved["url"] == "http://127.0.0.1:8090"
    assert saved["effort"] == "med"

    # 2. dev-server.sh refuses reserved port 8090 on both start and stop
    for verb in ("start", "stop"):
        res_guard = subprocess.run(
            ["bash", dev_srv_path, verb, "8090"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert res_guard.returncode == 2
        assert "refusing port 8090" in res_guard.stderr


def test_expired_auth_fast_503_for_agy_and_claude(http_client):
    """Verify expired login on both agy (60s interactive OAuth stderr) and claude (is_error result) fails fast (<3s) with HTTP 503 and closes dead session."""
    # 1. agy: emits 'Authentication required' on stderr and sleeps 60s -> _drain_stderr kills immediately & returns 503
    t0 = time.time()
    r_agy = http_client.post(
        "/v1/sessions",
        headers={"X-API-Key": "key-alice"},
        json={"agent": "mock_agy_session", "prompt": "AUTH_EXPIRED"},
    )
    elapsed_agy = time.time() - t0
    assert r_agy.status_code == 503, r_agy.text
    assert "authentication expired or missing" in r_agy.json()["detail"].lower()
    assert elapsed_agy < 5.0, f"agy auth abort took too long: {elapsed_agy:.2f}s"

    # 2. claude: emits is_error=True with 'Not logged in · Please run /login' in result -> returns 503 with message preserved
    r_claude = http_client.post(
        "/v1/sessions",
        headers={"X-API-Key": "key-alice"},
        json={"agent": "mock_claude_session", "prompt": "AUTH_EXPIRED"},
    )
    assert r_claude.status_code == 503, r_claude.text
    assert "please run /login" in r_claude.json()["detail"].lower()
    assert session_manager.active_session_count() == 0


def test_session_max_lifetime_and_default_effort(http_client, monkeypatch):
    """Verify SESSION_MAX_LIFETIME / max_lifetime expires sessions even when recently active, and DEFAULT_EFFORT applies."""
    monkeypatch.setenv("DEFAULT_EFFORT", "med")

    r1 = http_client.post(
        "/v1/sessions",
        headers={"X-API-Key": "key-alice"},
        json={"agent": "mock_agy_session", "idle_timeout": 900, "max_lifetime": 5},
    )
    assert r1.status_code == 201, r1.text
    d1 = r1.json()
    sid = d1["id"]
    assert d1["effort"] == "medium"
    assert d1["max_lifetime"] == 5
    assert d1.get("max_expires_at") is not None

    # Keep last_active_at fresh, but advance wall-clock age past max_lifetime
    live = session_manager._sessions[sid]
    ws_path = live.workspace_path
    live.last_active_at = time.time()
    live.created_at = time.time() - 7.0

    expired_doc = _poll_until_expired(http_client, sid, {"X-API-Key": "key-alice"})
    assert expired_doc["status"] == "expired"
    assert not os.path.exists(ws_path)


def test_coderabbit_pr7_review_fixes(http_client, monkeypatch, tmp_path):
    """Regression suite covering PR #7 review findings across jobs, db, pagination, security, and CLI."""
    import io
    import json
    import subprocess
    import sys

    # 1. /v1/jobs rejects session-only mock agents whose argv_builder is None with HTTP 400
    r_job = http_client.post(
        "/v1/jobs",
        headers={"X-API-Key": "key-alice"},
        json={"agent": "mock_agy_session", "prompt": "Should fail 400"},
    )
    assert r_job.status_code == 400
    assert "does not support one-shot jobs" in r_job.json()["detail"]

    # 2. Multipart file upload passed as scalar 'prompt' field rejected with 400 before spawning session
    r_bad_mp = http_client.post(
        "/v1/sessions",
        headers={"X-API-Key": "key-alice"},
        data={"agent": "mock_agy_session"},
        files={"prompt": ("evil.txt", io.BytesIO(b"hello"), "text/plain")},
    )
    assert r_bad_mp.status_code == 400
    assert "scalar text" in r_bad_mp.json()["detail"]
    assert session_manager.active_session_count() == 0

    # 3. session.error clears after subsequent successful turn, and prompt paths are NOT probed on host
    r_spawn = http_client.post(
        "/v1/sessions",
        headers={"X-API-Key": "key-alice"},
        json={"agent": "mock_agy_session"},
    )
    assert r_spawn.status_code == 201
    sid = r_spawn.json()["id"]

    r_s = http_client.post(
        f"/v1/sessions/{sid}/messages",
        headers={"X-API-Key": "key-alice"},
        json={"prompt": "SOFT_ERROR"},
    )
    assert r_s.status_code == 500
    s_after_err = http_client.get(f"/v1/sessions/{sid}", headers={"X-API-Key": "key-alice"}).json()
    assert s_after_err["error"] == "Transient tool execution failure"
    assert s_after_err["status"] == "idle"

    # Free-form prompt containing 'Attached files:\n- /nonexistent/host/secret' must NOT trigger missing-attachment 500
    r_rec = http_client.post(
        f"/v1/sessions/{sid}/messages",
        headers={"X-API-Key": "key-alice"},
        json={"prompt": "Normal prompt\n\nAttached files:\n- /nonexistent/host/secret.txt"},
    )
    assert r_rec.status_code == 200, r_rec.text
    assert r_rec.json()["error"] is None

    # Add one more turn and verify cursor pagination on GET ?include_turns=true
    r_t3 = http_client.post(
        f"/v1/sessions/{sid}/messages",
        headers={"X-API-Key": "key-alice"},
        json={"prompt": "Turn three"},
    )
    assert r_t3.status_code == 200

    p1 = http_client.get(
        f"/v1/sessions/{sid}?include_turns=true&turns_limit=2",
        headers={"X-API-Key": "key-alice"},
    ).json()
    assert len(p1["turns"]) == 2
    assert p1["turns_has_more"] is True
    assert p1["next_after_turn"] == 1

    p2 = http_client.get(
        f"/v1/sessions/{sid}?include_turns=true&turns_limit=2&after_turn={p1['next_after_turn']}",
        headers={"X-API-Key": "key-alice"},
    ).json()
    assert len(p2["turns"]) == 1
    assert p2["turns"][0]["turn_index"] == 2
    assert p2["turns_has_more"] is False

    # 4. WAIT_DEFAULT=0 honoured when JSON omits wait, returning 202 with submitted_turn_id and max_expires_at
    monkeypatch.setenv("WAIT_DEFAULT", "0")
    r_async = http_client.post(
        f"/v1/sessions/{sid}/messages",
        headers={"X-API-Key": "key-alice"},
        json={"prompt": "Async via WAIT_DEFAULT=0"},
    )
    assert r_async.status_code == 202
    d_async = r_async.json()
    assert d_async.get("submitted_turn_id") is not None
    assert d_async.get("max_expires_at") is not None
    monkeypatch.delenv("WAIT_DEFAULT", raising=False)

    # Wait briefly for async turn to finish, then test NON_DICT_JSON stream failure finalizes & closes session
    for _ in range(40):
        g = http_client.get(f"/v1/sessions/{sid}", headers={"X-API-Key": "key-alice"}).json()
        if g["turn_count"] >= 4 and g["status"] == "idle":
            break
        time.sleep(0.05)

    r_bad_stream = http_client.post(
        f"/v1/sessions/{sid}/messages",
        headers={"X-API-Key": "key-alice"},
        json={"prompt": "NON_DICT_JSON"},
    )
    assert r_bad_stream.status_code == 500
    assert not session_manager.is_session_active(sid)
    g_closed = http_client.get(f"/v1/sessions/{sid}", headers={"X-API-Key": "key-alice"}).json()
    assert g_closed["status"] == "failed"

    # 5. Dead idle child retired on /messages (frees active slot)
    r_dead = http_client.post(
        "/v1/sessions",
        headers={"X-API-Key": "key-alice"},
        json={"agent": "mock_agy_session"},
    )
    dead_sid = r_dead.json()["id"]
    dead_live = session_manager._sessions[dead_sid]
    dead_live.proc.kill()
    for _ in range(40):
        if dead_live.proc.returncode is not None:
            break
        time.sleep(0.02)
    r_dead_msg = http_client.post(
        f"/v1/sessions/{dead_sid}/messages",
        headers={"X-API-Key": "key-alice"},
        json={"prompt": "Child already dead"},
    )
    assert r_dead_msg.status_code == 409
    assert not session_manager.is_session_active(dead_sid)

    # 6. session-cli --close preserves cached session_id and returns nonzero on non-200/404 response
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    cli_path = os.path.join(repo_root, "scripts", "session-cli")
    st_file = str(tmp_path / "preserve-close.json")
    with open(st_file, "w", encoding="utf-8") as f:
        json.dump(
            {
                "url": "http://127.0.0.1:9",  # connection refused -> non-200
                "api_key": "bad",
                "session_id": "keep-this-id-1234",
            },
            f,
        )
    os.chmod(st_file, 0o600)
    res_close = subprocess.run(
        [sys.executable, cli_path, "--state-file", st_file, "--close"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_close.returncode != 0
    assert json.load(open(st_file))["session_id"] == "keep-this-id-1234"


def test_coderabbit_followup_6_threads(http_client, monkeypatch, tmp_path):
    """Covers all 6 CodeRabbit follow-up verification requests:
    1. Multi-turn mock_agy_session & mock_claude_session with BWRAP_ENABLED=1 (4714711356)
    2. Streamed multipart per-file & cumulative limits without Content-Length on both endpoints (4714711391)
    3. Split stderr auth marker & <=240-char persisted turn/session error+stderr diagnostics (4714711406)
    4. Unreaped child retains MAX_ACTIVE_SESSIONS capacity when termination fails (4714711426)
    5. DELETE cancels & joins blocked upload before workspace delete and returns 409 (4714711445)
    6. Partial upload rollback retains & persists bytes when os.remove fails (4714711458)
    """
    import asyncio
    import base64
    import shutil
    import threading
    import app.sessions as sessions_mod

    # --- 1. Multi-turn mock_agy_session & mock_claude_session under BWRAP_ENABLED=1 ---
    if shutil.which("bwrap"):
        monkeypatch.setenv("BWRAP_ENABLED", "1")
        monkeypatch.setenv("ALLOW_UNCONFINED", "0")
        for mock_agent in ("mock_agy_session", "mock_claude_session"):
            rb1 = http_client.post(
                "/v1/sessions",
                headers={"X-API-Key": "key-alice"},
                json={"agent": mock_agent, "prompt": f"Bwrap Turn 1 ({mock_agent})"},
            )
            assert rb1.status_code == 200, rb1.text
            bsid = rb1.json()["id"]
            rb2 = http_client.post(
                f"/v1/sessions/{bsid}/messages",
                headers={"X-API-Key": "key-alice"},
                json={"prompt": f"Bwrap Turn 2 ({mock_agent})"},
            )
            assert rb2.status_code == 200, rb2.text
            assert "Turn 2" in rb2.json()["latest_turn"]["response"]
            http_client.delete(f"/v1/sessions/{bsid}", headers={"X-API-Key": "key-alice"})
        monkeypatch.setenv("BWRAP_ENABLED", "0")

    # --- 2. Streamed multipart limits without Content-Length on both endpoints ---
    rate_limiter.history.clear()
    monkeypatch.setenv("MAX_ATTACHMENT_BYTES", "20")
    monkeypatch.setenv("MAX_TOTAL_BYTES", "30")

    boundary = "----TestStreamBoundary991"

    def make_multipart_chunks(fields_dict, files_list):
        parts = []
        for k, v in fields_dict.items():
            parts.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode("utf-8")
            )
        for fname, raw_bytes in files_list:
            header = (
                f"--{boundary}\r\n"
                f"Content-Disposition: form-data; name=\"file\"; filename=\"{fname}\"\r\n"
                f"Content-Type: application/octet-stream\r\n\r\n"
            ).encode("utf-8")
            parts.append(header)
            # Yield body in small 7-byte chunks so streaming parser sees incremental chunks without Content-Length
            for idx in range(0, len(raw_bytes), 7):
                parts.append(raw_bytes[idx : idx + 7])
            parts.append(b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode("utf-8"))
        for chunk in parts:
            yield chunk

    # 2a. Per-file overflow (25 > 20) streamed to POST /v1/sessions
    r_mp_create_over = http_client.post(
        "/v1/sessions",
        headers={"X-API-Key": "key-alice", "Content-Type": f"multipart/form-data; boundary={boundary}"},
        content=make_multipart_chunks({"agent": "mock_agy_session", "prompt": "hi"}, [("big.bin", b"A" * 25)]),
    )
    assert r_mp_create_over.status_code == 400
    assert "max single file size limit" in r_mp_create_over.json()["detail"]

    # 2b. Cumulative overflow (18 + 18 = 36 > 30) streamed to POST /v1/sessions
    r_mp_create_cum = http_client.post(
        "/v1/sessions",
        headers={"X-API-Key": "key-alice", "Content-Type": f"multipart/form-data; boundary={boundary}"},
        content=make_multipart_chunks(
            {"agent": "mock_agy_session", "prompt": "hi"},
            [("f1.bin", b"B" * 18), ("f2.bin", b"C" * 18)],
        ),
    )
    assert r_mp_create_cum.status_code == 400
    assert "MAX_TOTAL_BYTES" in r_mp_create_cum.json()["detail"]

    # 2c. Valid 18-byte upload on create, then streamed per-file & cumulative overflow on POST /messages
    r_mp_ok = http_client.post(
        "/v1/sessions",
        headers={"X-API-Key": "key-alice", "Content-Type": f"multipart/form-data; boundary={boundary}"},
        content=make_multipart_chunks({"agent": "mock_agy_session"}, [("ok.bin", b"D" * 18)]),
    )
    assert r_mp_ok.status_code == 201, r_mp_ok.text
    mp_sid = r_mp_ok.json()["id"]

    r_mp_msg_over = http_client.post(
        f"/v1/sessions/{mp_sid}/messages",
        headers={"X-API-Key": "key-alice", "Content-Type": f"multipart/form-data; boundary={boundary}"},
        content=make_multipart_chunks({"prompt": "hi"}, [("big2.bin", b"E" * 25)]),
    )
    assert r_mp_msg_over.status_code == 400
    assert "max single file size limit" in r_mp_msg_over.json()["detail"]

    r_mp_msg_cum = http_client.post(
        f"/v1/sessions/{mp_sid}/messages",
        headers={"X-API-Key": "key-alice", "Content-Type": f"multipart/form-data; boundary={boundary}"},
        content=make_multipart_chunks({"prompt": "hi"}, [("add.bin", b"F" * 15)]),  # 18 + 15 = 33 > 30
    )
    assert r_mp_msg_cum.status_code == 400
    assert "MAX_TOTAL_BYTES" in r_mp_msg_cum.json()["detail"]
    http_client.delete(f"/v1/sessions/{mp_sid}", headers={"X-API-Key": "key-alice"})

    monkeypatch.delenv("MAX_ATTACHMENT_BYTES", raising=False)
    monkeypatch.delenv("MAX_TOTAL_BYTES", raising=False)

    # --- 3. Split stderr auth marker & <=240-char persisted diagnostics ---
    rate_limiter.history.clear()
    r_sp = http_client.post(
        "/v1/sessions",
        headers={"X-API-Key": "key-alice"},
        json={"agent": "mock_agy_session"},
    )
    sp_sid = r_sp.json()["id"]
    r_sp_auth = http_client.post(
        f"/v1/sessions/{sp_sid}/messages",
        headers={"X-API-Key": "key-alice"},
        json={"prompt": "AUTH_EXPIRED SPLIT_LONG"},
    )
    assert r_sp_auth.status_code == 503
    assert len(r_sp_auth.json()["detail"]) <= 240
    sp_doc = http_client.get(
        f"/v1/sessions/{sp_sid}?include_turns=true",
        headers={"X-API-Key": "key-alice"},
    ).json()
    assert len(sp_doc["error"] or "") <= 240
    assert len(sp_doc["turns"][0]["error"] or "") <= 240
    assert len(sp_doc["turns"][0]["stderr"] or "") <= 240

    # Also test long Claude result auth error bounded <= 240 chars in persisted turn & session
    r_cl_sp = http_client.post(
        "/v1/sessions",
        headers={"X-API-Key": "key-alice"},
        json={"agent": "mock_claude_session"},
    )
    cl_sid = r_cl_sp.json()["id"]
    r_cl_auth = http_client.post(
        f"/v1/sessions/{cl_sid}/messages",
        headers={"X-API-Key": "key-alice"},
        json={"prompt": "SOFT_ERROR " + ("Z" * 600)},
    )
    assert r_cl_auth.status_code == 500
    cl_doc = http_client.get(
        f"/v1/sessions/{cl_sid}?include_turns=true",
        headers={"X-API-Key": "key-alice"},
    ).json()
    assert len(cl_doc["error"] or "") <= 240
    assert len(cl_doc["turns"][0]["error"] or "") <= 240
    http_client.delete(f"/v1/sessions/{cl_sid}", headers={"X-API-Key": "key-alice"})

    # --- 4. Simulated termination failure retains MAX_ACTIVE_SESSIONS capacity until child exit ---
    rate_limiter.history.clear()
    monkeypatch.setenv("MAX_ACTIVE_SESSIONS", "1")
    r_cap1 = http_client.post(
        "/v1/sessions",
        headers={"X-API-Key": "key-alice"},
        json={"agent": "mock_agy_session"},
    )
    assert r_cap1.status_code == 201
    cap_sid = r_cap1.json()["id"]
    cap_proc = session_manager._sessions[cap_sid].proc
    # Hold live.lock lookalike? No, force clean_exit=False by closing with reason='failed' or monkeypatching stdin & _terminate_and_reap_proc
    orig_term = sessions_mod._terminate_and_reap_proc

    async def noop_terminate(proc):
        return None

    monkeypatch.setattr(sessions_mod, "_terminate_and_reap_proc", noop_terminate)
    # Also prevent stdin EOF clean exit so proc stays alive
    monkeypatch.setattr(cap_proc.stdin, "close", lambda: None)

    r_del_cap = http_client.delete(f"/v1/sessions/{cap_sid}", headers={"X-API-Key": "key-alice"})
    assert r_del_cap.status_code == 200
    assert cap_proc.returncode is None
    assert session_manager.active_session_count() == 1

    # Attempt to create a new session while unkilled child is still running -> rejected with 429!
    r_cap2 = http_client.post(
        "/v1/sessions",
        headers={"X-API-Key": "key-alice"},
        json={"agent": "mock_agy_session"},
    )
    assert r_cap2.status_code == 429

    # Now actually terminate child -> capacity is released!
    monkeypatch.setattr(sessions_mod, "_terminate_and_reap_proc", orig_term)
    cap_proc.kill()
    for _ in range(50):
        h = http_client.get("/healthz").json()
        if h["active_sessions"] == 0 and cap_proc.returncode is not None:
            break
        time.sleep(0.02)
    assert cap_proc.returncode is not None
    monkeypatch.delenv("MAX_ACTIVE_SESSIONS", raising=False)

    # --- 5. Blocked attachment upload cancelled & joined on DELETE (returns 409, workspace stays deleted) ---
    rate_limiter.history.clear()
    r_blk = http_client.post(
        "/v1/sessions",
        headers={"X-API-Key": "key-alice"},
        json={"agent": "mock_agy_session"},
    )
    blk_sid = r_blk.json()["id"]
    blk_ws = session_manager._sessions[blk_sid].workspace_path
    upload_entered = threading.Event()
    upload_cancelled = threading.Event()
    orig_mat = sessions_mod.materialize_attachment

    async def slow_materialize(spec, workspace_path, total_bytes_so_far=0, allowlist=None):
        upload_entered.set()
        try:
            await asyncio.sleep(10.0)
            return await orig_mat(spec, workspace_path, total_bytes_so_far=total_bytes_so_far, allowlist=allowlist)
        except asyncio.CancelledError:
            upload_cancelled.set()
            raise

    monkeypatch.setattr(sessions_mod, "materialize_attachment", slow_materialize)

    with ThreadPoolExecutor(max_workers=2) as ex:
        fut_msg = ex.submit(
            http_client.post,
            f"/v1/sessions/{blk_sid}/messages",
            headers={"X-API-Key": "key-alice"},
            json={
                "prompt": "Slow upload",
                "attachments": [{"filename": "slow.txt", "content_b64": base64.b64encode(b"hi").decode("ascii")}],
            },
        )
        assert upload_entered.wait(timeout=3.0)
        r_del_blk = http_client.delete(f"/v1/sessions/{blk_sid}", headers={"X-API-Key": "key-alice"})
        assert r_del_blk.status_code == 200
        res_msg_blk = fut_msg.result(timeout=5.0)

    assert upload_cancelled.is_set()
    assert res_msg_blk.status_code == 409, res_msg_blk.text
    assert not os.path.exists(blk_ws)
    monkeypatch.setattr(sessions_mod, "materialize_attachment", orig_mat)

    # --- 6. Partial upload rollback retains & persists bytes when os.remove raises OSError ---
    monkeypatch.setenv("MAX_ATTACHMENT_BYTES", "10")
    r_rm = http_client.post(
        "/v1/sessions",
        headers={"X-API-Key": "key-alice"},
        json={"agent": "mock_agy_session"},
    )
    rm_sid = r_rm.json()["id"]

    orig_remove = os.remove

    def failing_remove(path, *a, **kw):
        if "keep8.bin" in os.path.basename(path):
            raise PermissionError("simulated unlink failure")
        return orig_remove(path, *a, **kw)

    monkeypatch.setattr(os, "remove", failing_remove)
    r_partial = http_client.post(
        f"/v1/sessions/{rm_sid}/messages",
        headers={"X-API-Key": "key-alice"},
        json={
            "prompt": "Two files, second too big",
            "attachments": [
                {"filename": "keep8.bin", "content_b64": base64.b64encode(b"12345678").decode("ascii")},
                {"filename": "toobig.bin", "content_b64": base64.b64encode(b"123456789012345").decode("ascii")},
            ],
        },
    )
    monkeypatch.setattr(os, "remove", orig_remove)
    monkeypatch.delenv("MAX_ATTACHMENT_BYTES", raising=False)

    assert r_partial.status_code == 400
    rm_doc = http_client.get(f"/v1/sessions/{rm_sid}", headers={"X-API-Key": "key-alice"}).json()
    assert rm_doc["status"] == "idle"
    assert rm_doc["total_attachment_bytes"] == 8
    http_client.delete(f"/v1/sessions/{rm_sid}", headers={"X-API-Key": "key-alice"})


@pytest.mark.asyncio
async def test_raw_asgi_incremental_multipart_early_rejection(tmp_path, monkeypatch):
    """4714711391: Send multiple raw ASGI http.request messages without Content-Length and verify early 400 before consuming trailing body."""
    rate_limiter.history.clear()
    test_db = str(tmp_path / "asgi_mp.db")
    sess_root = str(tmp_path / "asgi_sess")
    os.makedirs(sess_root, exist_ok=True)
    monkeypatch.setenv("DB_PATH", test_db)
    monkeypatch.setenv("SESSION_WORK_ROOT", sess_root)
    monkeypatch.setenv("API_KEY", "test-secret-key")
    monkeypatch.setenv("API_KEYS", "alice:key-alice:jobs")
    monkeypatch.setenv("ALLOW_MOCK_AGENT", "1")
    monkeypatch.setenv("BWRAP_ENABLED", "0")
    monkeypatch.setenv("MAX_ATTACHMENT_BYTES", "16")
    monkeypatch.setenv("MAX_TOTAL_BYTES", "24")
    init_db(test_db)

    boundary = "----RawAsgiBoundary77"

    async def run_raw_asgi_multipart(path: str, file_chunks: list):
        header_part = (
            f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"agent\"\r\n\r\nmock_agy_session\r\n"
            f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"prompt\"\r\n\r\nhello\r\n"
            f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"file\"; filename=\"streamed.bin\"\r\n"
            f"Content-Type: application/octet-stream\r\n\r\n"
        ).encode("utf-8")
        trailer_part = f"\r\n--{boundary}--\r\n".encode("utf-8")

        messages = [{"type": "http.request", "body": header_part, "more_body": True}]
        for ch in file_chunks:
            messages.append({"type": "http.request", "body": ch, "more_body": True})
        messages.append({"type": "http.request", "body": trailer_part, "more_body": False})

        idx = 0
        trailing_sentinel_consumed = False

        async def receive():
            nonlocal idx, trailing_sentinel_consumed
            if idx >= len(messages):
                return {"type": "http.disconnect"}
            msg = messages[idx]
            if idx == len(messages) - 2:  # last file chunk (sentinel)
                trailing_sentinel_consumed = True
            idx += 1
            return msg

        sent_events = []

        async def send(event):
            sent_events.append(event)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "headers": [
                (b"x-api-key", b"key-alice"),
                (b"content-type", f"multipart/form-data; boundary={boundary}".encode("ascii")),
            ],
            "client": ("127.0.0.1", 51000),
            "server": ("testserver", 80),
        }
        await app(scope, receive, send)
        start_evt = next(e for e in sent_events if e["type"] == "http.response.start")
        body_bytes = b"".join(e.get("body", b"") for e in sent_events if e["type"] == "http.response.body")
        return start_evt["status"], body_bytes.decode("utf-8"), trailing_sentinel_consumed

    # 1. POST /v1/sessions rejects after 2nd 10-byte chunk (20 > 16) BEFORE reading 3rd sentinel chunk!
    st1, body1, consumed1 = await run_raw_asgi_multipart(
        "/v1/sessions",
        [b"0123456789", b"abcdefghij", b"SENTINEL_NEVER_READ" * 100],
    )
    assert st1 == 400, body1
    assert "max single file size limit" in body1
    assert consumed1 is False

    # 2. Spawn a session with 14 bytes, then test cumulative overflow (14 + 12 = 26 > 24) on POST /messages before sentinel!
    live = await session_manager.create_session(
        agent="mock_agy_session",
        api_key_name="alice",
        db_path=test_db,
    )
    try:
        live.total_attachment_bytes = 14
        from app.db import update_session_state
        update_session_state(live.id, total_attachment_bytes=14, db_path=test_db)

        st2, body2, consumed2 = await run_raw_asgi_multipart(
            f"/v1/sessions/{live.id}/messages",
            [b"012345", b"6789ab", b"SENTINEL_NEVER_READ" * 100],
        )
        assert st2 == 400, body2
        assert "MAX_TOTAL_BYTES" in body2
        assert consumed2 is False
    finally:
        await session_manager.close_session(live.id, reason="closed", db_path=test_db)


@pytest.mark.asyncio
async def test_cancelled_creation_cleanup_retains_capacity_and_split_stderr(tmp_path, monkeypatch):
    """4714711426 & 4714711406: Cancelled creation cleanup retains capacity while child is alive; deterministic split stderr read."""
    import asyncio
    import collections
    import app.sessions as sessions_mod
    from app.sessions import _drain_stderr, LiveSession, AgyStreamDriver

    test_db = str(tmp_path / "cancel_cap.db")
    sess_root = str(tmp_path / "cancel_sess")
    os.makedirs(sess_root, exist_ok=True)
    monkeypatch.setenv("DB_PATH", test_db)
    monkeypatch.setenv("SESSION_WORK_ROOT", sess_root)
    monkeypatch.setenv("API_KEY", "dev")
    monkeypatch.setenv("ALLOW_MOCK_AGENT", "1")
    monkeypatch.setenv("BWRAP_ENABLED", "0")
    monkeypatch.setenv("MAX_ACTIVE_SESSIONS", "1")
    init_db(test_db)

    entered_cleanup = asyncio.Event()
    spawned_procs = []
    orig_exec = asyncio.create_subprocess_exec

    async def tracking_exec(*args, **kwargs):
        p = await orig_exec(*args, **kwargs)
        spawned_procs.append(p)
        return p

    def failing_create_row(*args, **kwargs):
        raise RuntimeError("simulated DB insert failure")

    orig_term = sessions_mod._terminate_and_reap_proc

    async def slow_term(proc):
        entered_cleanup.set()
        await asyncio.sleep(10.0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", tracking_exec)
    monkeypatch.setattr(sessions_mod, "create_session_row", failing_create_row)
    monkeypatch.setattr(sessions_mod, "_terminate_and_reap_proc", slow_term)

    create_task = asyncio.create_task(
        session_manager.create_session(agent="mock_agy_session", db_path=test_db)
    )
    await asyncio.wait_for(entered_cleanup.wait(), timeout=3.0)
    create_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await create_task

    assert len(spawned_procs) == 1
    leaked_proc = spawned_procs[0]
    assert leaked_proc.returncode is None
    assert session_manager.active_session_count() == 1

    # Restore create_session_row & _terminate_and_reap_proc; new session MUST be rejected (429) while leaked_proc is alive
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc_info:
        await session_manager.create_session(agent="mock_agy_session", db_path=test_db)
    assert exc_info.value.status_code == 429

    monkeypatch.setattr(sessions_mod, "_terminate_and_reap_proc", orig_term)
    await orig_term(leaked_proc)
    assert session_manager.active_session_count() == 0

    # Deterministic two-chunk split stderr unit test for _drain_stderr
    class FakeStderr:
        def __init__(self):
            self._chunks = [b"prefix\nAuthenti", b"cation required: " + (b"Z" * 500) + b"\n", b""]

        async def read(self, n):
            await asyncio.sleep(0.005)
            return self._chunks.pop(0) if self._chunks else b""

    killed = []

    class FakeProc:
        pid = 99999999
        returncode = None
        stderr = FakeStderr()

        def kill(self):
            killed.append(True)
            self.returncode = -9

    dummy_live = LiveSession("s1", "agy", None, None, str(tmp_path), FakeProc(), AgyStreamDriver(), 60, 60, "alice")
    await _drain_stderr(dummy_live.proc, collections.deque(maxlen=20), dummy_live)
    assert killed == [True]
    assert dummy_live.auth_abort_reason is not None
    assert len(dummy_live.auth_abort_reason) <= 240


def test_dev_server_and_session_cli_security_and_race_guards(http_client, monkeypatch, tmp_path):
    """4714717249, 4714717258, 4714717273, 4714717280, 4714711406: dev-server & session-cli edge cases."""
    import importlib.machinery
    import importlib.util
    import json
    import socket
    import subprocess
    import sys

    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    dev_srv = os.path.join(repo_root, "scripts", "dev-server.sh")
    cli_path = os.path.join(repo_root, "scripts", "session-cli")

    # 1. Upstream 600-char auth error via LONG_AUTH is bounded <= 240 chars in persisted turn & session
    rate_limiter.history.clear()
    r_la = http_client.post(
        "/v1/sessions",
        headers={"X-API-Key": "key-alice"},
        json={"agent": "mock_claude_session"},
    )
    la_sid = r_la.json()["id"]
    r_la_turn = http_client.post(
        f"/v1/sessions/{la_sid}/messages",
        headers={"X-API-Key": "key-alice"},
        json={"prompt": "SOFT_ERROR LONG_AUTH"},
    )
    assert r_la_turn.status_code == 503
    assert len(r_la_turn.json()["detail"]) <= 240
    la_doc = http_client.get(f"/v1/sessions/{la_sid}?include_turns=true", headers={"X-API-Key": "key-alice"}).json()
    assert len(la_doc["error"] or "") <= 240
    assert len(la_doc["turns"][0]["error"] or "") <= 240

    # 2. dev-server.sh rejects unsafe group/world-writable STATE_DIR BEFORE reading PID in both start and stop
    # 2. dev-server.sh isolated under tmp_path via DEV_SERVER_STATE_ROOT + HOME (never touches /tmp/agent-api-dev-*)
    import fcntl
    import shutil as _sh
    import urllib.error
    import urllib.request

    dev_state_root = tmp_path / "dev_srv_root"
    dev_state_root.mkdir(mode=0o700)
    dev_home = tmp_path / "dev_home"
    dev_home.mkdir(mode=0o700)

    dev_env = os.environ.copy()
    dev_env["DEV_SERVER_STATE_ROOT"] = str(dev_state_root)
    dev_env["HOME"] = str(dev_home)

    # 4715616308: Bind port 0 and keep 'occ' open for the entire set of non-live + occupied-port checks (zero port-reuse race)
    occ = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    occ.bind(("127.0.0.1", 0))
    occ.listen(5)
    test_port = occ.getsockname()[1]
    try:
        unsafe_dir = os.path.join(str(dev_state_root), f"agent-api-dev-{test_port}")
        os.mkdir(unsafe_dir, 0o777)
        os.chmod(unsafe_dir, 0o777)
        # Put current test process PID in uvicorn.pid — stop MUST refuse unsafe dir and NOT signal us!
        with open(os.path.join(unsafe_dir, "uvicorn.pid"), "w") as f:
            f.write(f"{os.getpid()}\n")
        for action in ("start", "stop", "status"):
            res = subprocess.run(["bash", dev_srv, action, str(test_port)], env=dev_env, capture_output=True, text=True, check=False)
            assert res.returncode != 0
            assert "refusing" in res.stderr

        # 4715131553(a): PID-only record inside valid 0700 state dir is rejected (exit != 0) without signaling os.getpid()
        _sh.rmtree(unsafe_dir, ignore_errors=True)
        os.mkdir(unsafe_dir, 0o700)
        pid_f = os.path.join(unsafe_dir, "uvicorn.pid")
        with open(pid_f, "w") as f:
            f.write(f"{os.getpid()}\n")
        os.chmod(pid_f, 0o600)
        res_pid_only = subprocess.run(["bash", dev_srv, "stop", str(test_port)], env=dev_env, capture_output=True, text=True, check=False)
        assert res_pid_only.returncode != 0
        assert "without valid '<pid>:<starttime>'" in res_pid_only.stderr

        # 4715131553(b): Valid <pid>:<starttime> when pidfd signaling is unavailable fails (exit != 0) without os.kill fallback
        my_starttime = open(f"/proc/{os.getpid()}/stat", encoding="utf-8").read().rsplit(")", 1)[1].split()[19]
        with open(pid_f, "w") as f:
            f.write(f"{os.getpid()}:{my_starttime}\n")
        os.chmod(pid_f, 0o600)
        no_pidfd_pkg = tmp_path / "no_pidfd_site"
        no_pidfd_pkg.mkdir(mode=0o700)
        (no_pidfd_pkg / "sitecustomize.py").write_text(
            "import os, signal\n"
            "if hasattr(os, 'pidfd_open'): delattr(os, 'pidfd_open')\n"
            "if hasattr(signal, 'pidfd_send_signal'): delattr(signal, 'pidfd_send_signal')\n",
            encoding="utf-8",
        )
        no_pidfd_env = dev_env.copy()
        no_pidfd_env["PYTHONPATH"] = str(no_pidfd_pkg) + (os.pathsep + dev_env["PYTHONPATH"] if dev_env.get("PYTHONPATH") else "")
        res_no_pidfd = subprocess.run(["bash", dev_srv, "stop", str(test_port)], env=no_pidfd_env, capture_output=True, text=True, check=False)
        assert res_no_pidfd.returncode != 0
        assert "pidfd signaling" in res_no_pidfd.stderr
        assert os.path.exists(pid_f)

        # 4715616296: pidfd_open raising transient OSError (EMFILE) returns non-cleanup error and preserves STATE_DIR & PID_FILE
        emfile_pkg = tmp_path / "emfile_pidfd_site"
        emfile_pkg.mkdir(mode=0o700)
        (emfile_pkg / "sitecustomize.py").write_text(
            "import errno, os\n"
            "def _emfile_pidfd(pid, flags=0):\n"
            "    raise OSError(errno.EMFILE, 'Too many open files')\n"
            "os.pidfd_open = _emfile_pidfd\n",
            encoding="utf-8",
        )
        emfile_env = dev_env.copy()
        emfile_env["PYTHONPATH"] = str(emfile_pkg) + (os.pathsep + dev_env["PYTHONPATH"] if dev_env.get("PYTHONPATH") else "")
        res_emfile = subprocess.run(["bash", dev_srv, "stop", str(test_port)], env=emfile_env, capture_output=True, text=True, check=False)
        assert res_emfile.returncode != 0
        assert os.path.exists(unsafe_dir) and os.path.exists(pid_f)

        # Stale/mismatched starttime in PID file is ignored without signaling, and cleans up state dir while preserving lock inode
        with open(pid_f, "w") as f:
            f.write(f"{os.getpid()}:0\n")
        os.chmod(pid_f, 0o600)
        res_stale = subprocess.run(["bash", dev_srv, "stop", str(test_port)], env=dev_env, capture_output=True, text=True, check=False)
        assert res_stale.returncode == 0
        assert "No running verified dev-server PID" in res_stale.stdout
        assert not os.path.exists(unsafe_dir)

        # 4715131544 & 4715616313: Instrumented flock wrapper proves stop reaches flock and blocks on LOCK_FILE when STATE_DIR is absent
        lock_path = os.path.join(str(dev_state_root), f".agent-api-dev-{test_port}.lock")
        assert os.path.exists(lock_path)
        lock_ino_before = os.stat(lock_path).st_ino
        flock_bin_dir = tmp_path / "flock_bin"
        flock_bin_dir.mkdir(mode=0o700)
        real_flock = _sh.which("flock")
        assert real_flock is not None
        flock_reached = tmp_path / "flock.reached"
        flock_wrapper = flock_bin_dir / "flock"
        flock_wrapper.write_text(
            f"#!/usr/bin/env bash\n: > {str(flock_reached)!r}\nexec {real_flock!r} \"$@\"\n",
            encoding="utf-8",
        )
        flock_wrapper.chmod(0o700)
        flock_env = dev_env.copy()
        flock_env["PATH"] = str(flock_bin_dir) + os.pathsep + dev_env.get("PATH", "")

        with open(lock_path, "r+b") as lock_fh:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            proc_stop = subprocess.Popen(
                ["bash", dev_srv, "stop", str(test_port)],
                env=flock_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                reach_deadline = time.time() + 5.0
                while time.time() < reach_deadline and not flock_reached.exists():
                    time.sleep(0.02)
                assert flock_reached.exists(), "stop child did not reach flock checkpoint"
                time.sleep(0.05)
                assert proc_stop.poll() is None, "stop child exited before lock was released"
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
                out_s, err_s = proc_stop.communicate(timeout=5.0)
                assert proc_stop.returncode == 0, f"stdout={out_s}\nstderr={err_s}"
            finally:
                if proc_stop.poll() is None:
                    proc_stop.kill()
                    proc_stop.wait()
        assert os.stat(lock_path).st_ino == lock_ino_before

        # Occupied port ('occ' still bound!) is rejected cleanly
        res_occ = subprocess.run(["bash", dev_srv, "start", str(test_port)], env=dev_env, capture_output=True, text=True, check=False)
        assert res_occ.returncode != 0
        assert "already in use" in res_occ.stderr
    finally:
        occ.close()

    # 4715131548 & 4715616299 & 4715616308: Allocate fresh port for live start (retrying only on bind collision) + test 0400 stale pid_file
    live_port = None
    res_start = None
    shadow_env = dev_env.copy()
    shadow_env["API_KEYS"] = "default:inherited-shadow-secret:jobs+admin"
    for _ in range(5):
        p_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        p_sock.bind(("127.0.0.1", 0))
        cand_live = p_sock.getsockname()[1]
        p_sock.close()
        if cand_live == 8090:
            continue
        # Pre-seed an owned mode-0400 stale uvicorn.pid to verify start replaces it cleanly (4715616299)
        cand_dir = dev_state_root / f"agent-api-dev-{cand_live}"
        cand_dir.mkdir(mode=0o700, exist_ok=True)
        stale_ro_pid = cand_dir / "uvicorn.pid"
        stale_ro_pid.write_text(f"{os.getpid()}:0\n", encoding="utf-8")
        stale_ro_pid.chmod(0o400)
        res_try = subprocess.run(["bash", dev_srv, "start", str(cand_live)], env=shadow_env, capture_output=True, text=True, check=False)
        if res_try.returncode != 0 and "already in use" in res_try.stderr:
            stale_ro_pid.chmod(0o600)
            _sh.rmtree(cand_dir, ignore_errors=True)
            continue
        live_port = cand_live
        res_start = res_try
        break

    assert live_port is not None and res_start is not None
    live_lock_path = os.path.join(str(dev_state_root), f".agent-api-dev-{live_port}.lock")
    live_lock_ino = os.stat(live_lock_path).st_ino
    try:
        assert res_start.returncode == 0, f"stdout={res_start.stdout}\nstderr={res_start.stderr}"
        saved_cli_cfg = json.loads((dev_home / ".cache" / "agent-api" / "session-cli.json").read_text(encoding="utf-8"))
        gen_key = saved_cli_cfg["api_key"]
        assert gen_key and gen_key != "inherited-shadow-secret"

        req_ok = urllib.request.Request(f"http://127.0.0.1:{live_port}/v1/me", headers={"X-API-Key": gen_key})
        with urllib.request.urlopen(req_ok, timeout=2.0) as resp_ok:
            assert resp_ok.status == 200

        req_bad = urllib.request.Request(
            f"http://127.0.0.1:{live_port}/v1/me", headers={"X-API-Key": "inherited-shadow-secret"}
        )
        with pytest.raises(urllib.error.HTTPError) as http_err:
            urllib.request.urlopen(req_bad, timeout=2.0)
        assert http_err.value.code == 401
    finally:
        res_stop_live = subprocess.run(["bash", dev_srv, "stop", str(live_port)], env=shadow_env, capture_output=True, text=True, check=False)
        assert res_stop_live.returncode == 0
        assert os.stat(live_lock_path).st_ino == live_lock_ino

    # 3. session-cli module unit tests: preserve session_id on 404/401 across --close, --new, REPL /new, and 202 polling
    loader = importlib.machinery.SourceFileLoader("session_cli_mod", cli_path)
    spec = importlib.util.spec_from_loader("session_cli_mod", loader)
    cli_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli_mod)

    st_path = str(tmp_path / "cli_unit.json")
    state = {
        "url": "http://testserver",
        "api_key": "key-bob",
        "agent": "mock_agy_session",
        "model": None,
        "effort": None,
        "idle_timeout": 900,
        "wait": 60,
        "session_id": "alice-owned-session-id",
    }
    cli_mod.save_state(st_path, state)

    # Mock api_request returning 404 (e.g. wrong tenant key) on DELETE and GET
    monkeypatch.setattr(cli_mod, "api_request", lambda *a, **kw: (404, {"detail": "Session not found"}))
    assert cli_mod.cmd_close(state, st_path) == 1
    assert state["session_id"] == "alice-owned-session-id"
    assert json.load(open(st_path))["session_id"] == "alice-owned-session-id"

    assert cli_mod.send_message_or_spawn(state, st_path, prompt="hi", force_new=True) is None
    assert state["session_id"] == "alice-owned-session-id"

    assert cli_mod.ensure_warm_idle_session(state, st_path) is None
    assert state["session_id"] == "alice-owned-session-id"

    # CLI subprocess with --new and no prompt exits 1 when DELETE returns 401/404 and preserves session_id
    bad_state_path = str(tmp_path / "cli_sub_new.json")
    cli_mod.save_state(bad_state_path, {**state, "url": "http://127.0.0.1:9"})
    res_new = subprocess.run(
        [sys.executable, cli_path, "--state-file", bad_state_path, "--new"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_new.returncode != 0
    assert json.load(open(bad_state_path))["session_id"] == "alice-owned-session-id"

    # 202 polling: rejects missing submitted_turn_id, and selects exact submitted_turn_id even when later turn completed
    assert cli_mod._poll_until_turn_done(state, "sid-1", time.time(), submitted_turn_id=None) is None

    monkeypatch.setattr(cli_mod.time, "sleep", lambda _: None)
    fake_turns = [
        {"id": "turn-A", "turn_index": 0, "status": "completed", "response": "Exact Turn A"},
        {"id": "turn-B", "turn_index": 1, "status": "completed", "response": "Later Turn B"},
    ]
    monkeypatch.setattr(
        cli_mod,
        "_fetch_all_session_turns",
        lambda st, sid: (200, {"id": sid, "status": "idle", "turn_count": 2}, fake_turns),
    )
    polled = cli_mod._poll_until_turn_done(state, "sid-1", time.time(), submitted_turn_id="turn-A")
    assert polled is not None
    assert polled["latest_turn"]["response"] == "Exact Turn A"
    # Footer uses matched turn_index + 1 (1/2) rather than session turn_count (2)
    footer = cli_mod.format_turn_footer(polled, 1.0)
    assert "turn=1/2" in footer

    # --forget clears local cached session_id without calling server
    assert cli_mod.cmd_forget(state, st_path) == 0
    assert state["session_id"] is None
    assert json.load(open(st_path))["session_id"] is None

    # 4. Work-root overlap guard, bypass key-name rejection, and DEFAULT_EFFORT non-persistence on jobs
    from app.runner import validate_distinct_work_roots
    from app.security import parse_api_keys
    with pytest.raises(ValueError):
        validate_distinct_work_roots(str(tmp_path / "same"), str(tmp_path / "same"))
    with pytest.raises(ValueError):
        validate_distinct_work_roots(str(tmp_path / "outer"), str(tmp_path / "outer" / "inner"))

    monkeypatch.setenv("API_KEYS", "bypass:evil-secret:jobs,alice:key-alice:jobs")
    parsed_keys = parse_api_keys()
    assert "bypass" not in parsed_keys
    # 4715131507 HTTP checks: rejected 'bypass' key gets 401; trusted local loopback identity without X-API-Key gets 200 unscoped access
    r_evil_http = http_client.get(f"/v1/sessions/{la_sid}", headers={"X-API-Key": "evil-secret"})
    assert r_evil_http.status_code == 401
    monkeypatch.setenv("TRUSTED_NETWORKS", "127.0.0.1/32")
    r_local_bypass = http_client.get(f"/v1/sessions/{la_sid}")
    assert r_local_bypass.status_code == 200
    assert r_local_bypass.json()["id"] == la_sid
    monkeypatch.delenv("TRUSTED_NETWORKS", raising=False)

    monkeypatch.setenv("DEFAULT_EFFORT", "high")
    r_j_def = http_client.post(
        "/v1/jobs",
        headers={"X-API-Key": "key-alice"},
        json={"agent": "mock_429", "prompt": "Check job effort row", "wait": 0},
    )
    assert r_j_def.status_code in (200, 202)
    assert r_j_def.json().get("effort") is None
    monkeypatch.delenv("DEFAULT_EFFORT", raising=False)

    # Oversized after_turn cursor (> SQLite INT64 max) rejected with 422 instead of 500 OverflowError
    r_ov_cur = http_client.get(
        f"/v1/sessions/{la_sid}?include_turns=true&after_turn={2**63}",
        headers={"X-API-Key": "key-alice"},
    )
    assert r_ov_cur.status_code == 422

    # 5. Spawn-only attachment FileNotFoundError: active session closes as 'failed' (500), concurrent close preserves 'closed' (409)
    import base64 as _b64
    import app.main as _main_mod
    from app.db import list_sessions as _list_sessions
    from app.sessions import session_manager as _sm
    from app.security import rate_limiter as _rl
    _rl.history.clear()

    real_materialize = _main_mod.materialize_attachment
    call_idx = {"n": 0, "ws": None}

    async def _fail_second_active(att_dict, workspace_path, **kwargs):
        call_idx["n"] += 1
        call_idx["ws"] = workspace_path
        if call_idx["n"] == 1:
            return await real_materialize(att_dict, workspace_path, **kwargs)
        raise FileNotFoundError("Simulated missing staging file on second attachment")

    monkeypatch.setattr(_main_mod, "materialize_attachment", _fail_second_active)
    b64_payload = _b64.b64encode(b"first-ok").decode("ascii")
    before_ids = {s["id"] for s in _list_sessions(limit=100)}
    r_fnf_active = http_client.post(
        "/v1/sessions",
        headers={"X-API-Key": "key-alice"},
        json={
            "agent": "mock_agy_session",
            "attachments": [
                {"filename": "first.txt", "content_b64": b64_payload},
                {"filename": "second.txt", "content_b64": b64_payload},
            ],
        },
    )
    assert r_fnf_active.status_code == 500
    assert call_idx["n"] == 2
    after_rows = [s for s in _list_sessions(limit=100) if s["id"] not in before_ids]
    assert len(after_rows) == 1
    assert after_rows[0]["status"] == "failed"
    assert not _sm.is_session_active(after_rows[0]["id"])
    assert call_idx["ws"] is not None and not os.path.exists(call_idx["ws"])

    # Separate test: concurrent closure during second attachment raises FileNotFoundError -> preserves 409 & 'closed' status
    conc_idx = {"n": 0, "sid": None}

    async def _fail_second_after_concurrent_close(att_dict, workspace_path, **kwargs):
        conc_idx["n"] += 1
        if conc_idx["n"] == 1:
            return await real_materialize(att_dict, workspace_path, **kwargs)
        sid = os.path.basename(workspace_path.rstrip("/"))
        conc_idx["sid"] = sid
        await _sm.close_session(sid, reason="closed", db_path=_main_mod.get_settings().db_path)
        raise FileNotFoundError("Workspace deleted by concurrent session close")

    monkeypatch.setattr(_main_mod, "materialize_attachment", _fail_second_after_concurrent_close)
    r_fnf_conc = http_client.post(
        "/v1/sessions",
        headers={"X-API-Key": "key-alice"},
        json={
            "agent": "mock_agy_session",
            "attachments": [
                {"filename": "first.txt", "content_b64": b64_payload},
                {"filename": "second.txt", "content_b64": b64_payload},
            ],
        },
    )
    assert r_fnf_conc.status_code == 409
    assert conc_idx["n"] == 2 and conc_idx["sid"] is not None
    conc_row = next(s for s in _list_sessions(limit=100) if s["id"] == conc_idx["sid"])
    assert conc_row["status"] == "closed"
    assert not _sm.is_session_active(conc_idx["sid"])
    monkeypatch.setattr(_main_mod, "materialize_attachment", real_materialize)

    # Also verify turn-level FileNotFoundError on an active session rolls back to 'idle' with 500 (not 409)
    import app.sessions as _sess_mod
    real_sess_mat = _sess_mod.materialize_attachment
    r_turn_s = http_client.post(
        "/v1/sessions",
        headers={"X-API-Key": "key-alice"},
        json={"agent": "mock_agy_session"},
    )
    assert r_turn_s.status_code == 201
    turn_sid = r_turn_s.json()["id"]
    t_idx = {"n": 0}

    async def _fail_turn_second(att_dict, workspace_path, **kwargs):
        t_idx["n"] += 1
        if t_idx["n"] == 1:
            return await real_sess_mat(att_dict, workspace_path, **kwargs)
        raise FileNotFoundError("Simulated missing staging file during turn attachment")

    monkeypatch.setattr(_sess_mod, "materialize_attachment", _fail_turn_second)
    r_turn_fnf = http_client.post(
        f"/v1/sessions/{turn_sid}/messages",
        headers={"X-API-Key": "key-alice"},
        json={
            "prompt": "Attach two files",
            "attachments": [
                {"filename": "t1.txt", "content_b64": b64_payload},
                {"filename": "t2.txt", "content_b64": b64_payload},
            ],
        },
    )
    assert r_turn_fnf.status_code == 500
    monkeypatch.setattr(_sess_mod, "materialize_attachment", real_sess_mat)
    r_turn_get = http_client.get(f"/v1/sessions/{turn_sid}", headers={"X-API-Key": "key-alice"})
    assert r_turn_get.status_code == 200
    assert r_turn_get.json()["status"] == "idle"
    assert r_turn_get.json()["total_attachment_bytes"] == 0
    http_client.delete(f"/v1/sessions/{turn_sid}", headers={"X-API-Key": "key-alice"})







def test_partial_attachment_write_and_db_outage_reconciliation(http_client, monkeypatch):
    """4715131526 & 4715131535: partial write cleanup + unremovable byte accounting, and live DB outage reconciliation."""
    import base64
    import builtins
    import errno
    import sqlite3
    import app.attachments as _att_mod
    import app.sessions as _sess_mod
    from app.db import get_session

    rate_limiter.history.clear()

    # --- 4715131526: Partial write + OSError cleanup and unremovable partial-file byte accounting ---
    r_spawn = http_client.post(
        "/v1/sessions",
        headers={"X-API-Key": "key-alice"},
        json={"agent": "mock_agy_session"},
    )
    assert r_spawn.status_code == 201
    sid_att = r_spawn.json()["id"]
    ws_att = session_manager._sessions[sid_att].workspace_path
    att_dir = os.path.join(ws_att, "attachments")

    real_open = builtins.open

    class _PartialFailWriter:
        def __init__(self, fh):
            self._fh = fh

        def write(self, data):
            self._fh.write(data[:11])
            self._fh.flush()
            raise OSError(errno.ENOSPC, "Simulated disk full after 11-byte partial write")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._fh.close()
            return False

    def _patched_open(file, mode="r", *args, **kwargs):
        fh = real_open(file, mode, *args, **kwargs)
        if "w" in mode and "b" in mode and (isinstance(file, int) or str(file).startswith(att_dir) or ".staging" in str(file)):
            return _PartialFailWriter(fh)
        return fh

    monkeypatch.setattr(_att_mod, "open", _patched_open, raising=False)
    payload_32 = base64.b64encode(b"A" * 32).decode("ascii")

    # Case A: unlink succeeds -> partial staging file removed and total_attachment_bytes stays 0
    r_part_clean = http_client.post(
        f"/v1/sessions/{sid_att}/messages",
        headers={"X-API-Key": "key-alice"},
        json={
            "prompt": "Try partial write A",
            "attachments": [{"filename": "partial_clean.bin", "content_b64": payload_32}],
        },
    )
    assert r_part_clean.status_code == 500
    assert os.listdir(att_dir) == []
    s_after_a = http_client.get(f"/v1/sessions/{sid_att}", headers={"X-API-Key": "key-alice"}).json()
    assert s_after_a["status"] == "idle"
    assert s_after_a["total_attachment_bytes"] == 0

    # Case B: unlink ALSO fails -> unremovable 11-byte partial staging file is counted in total_attachment_bytes
    real_unlink = os.unlink

    def _deny_unlink_unremovable(path, *args, **kwargs):
        if ".staging" in str(path) or os.path.basename(str(path)).startswith("unremovable"):
            raise PermissionError("Simulated unremovable partial file")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(_att_mod.os, "unlink", _deny_unlink_unremovable)
    r_part_stuck = http_client.post(
        f"/v1/sessions/{sid_att}/messages",
        headers={"X-API-Key": "key-alice"},
        json={
            "prompt": "Try partial write B",
            "attachments": [{"filename": "unremovable.bin", "content_b64": payload_32}],
        },
    )
    assert r_part_stuck.status_code == 500
    monkeypatch.setattr(_att_mod.os, "unlink", real_unlink)
    monkeypatch.delattr(_att_mod, "open", raising=False)

    s_after_b = http_client.get(f"/v1/sessions/{sid_att}", headers={"X-API-Key": "key-alice"}).json()
    assert s_after_b["status"] == "idle"
    assert s_after_b["total_attachment_bytes"] == 11

    # 4715131526: Earlier file in same turn becomes inaccessible (remove raises PermissionError where os.path.exists would be False)
    # followed by a 2nd attachment failing before creating any file -> rollback MUST retain the 32 bytes (no undercount)
    real_remove = os.remove

    def _deny_inaccessible_rollback(path, *args, **kwargs):
        if os.path.basename(str(path)).startswith("inaccessible_saved"):
            raise PermissionError(errno.EACCES, "Permission denied unlinking saved attachment")
        return real_remove(path, *args, **kwargs)

    real_path_exists = os.path.exists
    monkeypatch.setattr(_sess_mod.os, "remove", _deny_inaccessible_rollback)
    monkeypatch.setattr(_sess_mod.os.path, "exists", lambda p: False if "inaccessible_saved" in str(p) else real_path_exists(p))
    r_inacc_rb = http_client.post(
        f"/v1/sessions/{sid_att}/messages",
        headers={"X-API-Key": "key-alice"},
        json={
            "prompt": "First saves 32B then becomes inaccessible; second fails base64 decode",
            "attachments": [
                {"filename": "inaccessible_saved.bin", "content_b64": payload_32},
                {"filename": "bad_second.bin", "content_b64": "!!!not-base64!!!"},
            ],
        },
    )
    assert r_inacc_rb.status_code == 400
    monkeypatch.setattr(_sess_mod.os, "remove", real_remove)
    monkeypatch.setattr(_sess_mod.os.path, "exists", real_path_exists)
    s_after_inacc = http_client.get(f"/v1/sessions/{sid_att}", headers={"X-API-Key": "key-alice"}).json()
    assert s_after_inacc["status"] == "idle"
    assert s_after_inacc["total_attachment_bytes"] == 43  # 11 + 32 retained (no undercount!)

    # 4715616293 & 4715616289: End-to-end real fetch_url_attachment streaming tests:
    # (1) Pre-existing agent 'report.txt.tmp' is NOT truncated or unlinked on failing URL upload for 'report.txt'
    # (2) Concurrent agent files created mid-download (between >=8192B URL stream chunks) remain untouched when upload fails
    # (3) Repeated oversize URL downloads for the SAME filename with failed cleanup use distinct staging paths (no undercount)
    import httpx as _httpx

    agent_tmp = os.path.join(att_dir, "report.txt.tmp")
    with real_open(agent_tmp, "wb") as tf:
        tf.write(b"agent-tmp-payload")

    concurrent_agent_file = os.path.join(att_dir, "concurrent_during_stream.txt")
    concurrent_report_file = os.path.join(att_dir, "report.txt")

    class _StreamScenario(_httpx.AsyncByteStream):
        def __init__(self, mode: str):
            self.mode = mode

        async def __aiter__(self):
            if self.mode == "collision_fail":
                yield b"chunk-1-" * 1024  # 8192 bytes flushed as chunk 1
                raise _httpx.ReadError("Simulated mid-stream failure on report.txt")
            elif self.mode == "concurrent_create_fail":
                yield b"chunk-1-" * 1024  # 8192 bytes flushed as chunk 1
                # Agent writes files into attachments/ while URL download yielded!
                with real_open(concurrent_agent_file, "wb") as cf:
                    cf.write(b"agent-concurrent-data")
                with real_open(concurrent_report_file, "wb") as rf:
                    rf.write(b"agent-report-v1")
                raise _httpx.ReadError("Simulated stream cut after agent created files")
            elif self.mode == "oversize_1chunk":
                yield b"A" * 8192
                yield b"B" * 12000  # 20192 > MAX_ATTACHMENT_BYTES=18000 -> ValueError after 8192B written
            elif self.mode == "oversize_2chunks":
                yield b"C" * 8192
                yield b"D" * 8192
                yield b"E" * 8192  # 24576 > MAX_ATTACHMENT_BYTES=18000 -> ValueError after 16384B written

    def _mock_handler(request: _httpx.Request) -> _httpx.Response:
        q = request.url.params.get("case", "")
        return _httpx.Response(200, headers={"content-type": "text/plain"}, stream=_StreamScenario(q))

    real_async_client = _httpx.AsyncClient
    monkeypatch.setenv("ATTACHMENT_HOST_ALLOWLIST", "files.example.test")
    monkeypatch.setattr(
        _att_mod.httpx,
        "AsyncClient",
        lambda **kw: real_async_client(transport=_httpx.MockTransport(_mock_handler), **{k: v for k, v in kw.items() if k != "transport"}),
    )

    # (1) Temporary-path collision check on report.txt.tmp
    r_col = http_client.post(
        f"/v1/sessions/{sid_att}/messages",
        headers={"X-API-Key": "key-alice"},
        json={
            "prompt": "Download report.txt while report.txt.tmp exists",
            "attachments": [{"url": "https://files.example.test/report.txt?case=collision_fail"}],
        },
    )
    assert r_col.status_code == 502
    assert os.path.exists(agent_tmp)
    assert real_open(agent_tmp, "rb").read() == b"agent-tmp-payload"

    # (2) Concurrent agent file creation mid-stream
    rate_limiter.history.clear()
    r_conc_url = http_client.post(
        f"/v1/sessions/{sid_att}/messages",
        headers={"X-API-Key": "key-alice"},
        json={
            "prompt": "Download report.txt while agent creates files mid-stream",
            "attachments": [{"url": "https://files.example.test/report.txt?case=concurrent_create_fail"}],
        },
    )
    assert r_conc_url.status_code == 502
    assert os.path.exists(concurrent_agent_file) and real_open(concurrent_agent_file, "rb").read() == b"agent-concurrent-data"
    assert os.path.exists(concurrent_report_file) and real_open(concurrent_report_file, "rb").read() == b"agent-report-v1"

    # (3) Repeated oversize URL downloads for the SAME filename ('report.bin') when staging unlink fails:
    # first retains 8192B (43 + 8192 = 8235), second retains 16384B in a DISTINCT staging file (8235 + 16384 = 24619, zero undercount!)
    monkeypatch.setenv("MAX_ATTACHMENT_BYTES", "10000")
    monkeypatch.setattr(_att_mod.os, "unlink", _deny_unlink_unremovable)

    r_ov_1 = http_client.post(
        f"/v1/sessions/{sid_att}/messages",
        headers={"X-API-Key": "key-alice"},
        json={
            "prompt": "Oversize same filename #1",
            "attachments": [{"url": "https://files.example.test/report.bin?case=oversize_1chunk"}],
        },
    )
    assert r_ov_1.status_code == 400
    s_ov_1 = http_client.get(f"/v1/sessions/{sid_att}", headers={"X-API-Key": "key-alice"}).json()
    assert s_ov_1["status"] == "idle"
    assert s_ov_1["total_attachment_bytes"] == 43 + 8192

    monkeypatch.setenv("MAX_ATTACHMENT_BYTES", "20000")
    r_ov_2 = http_client.post(
        f"/v1/sessions/{sid_att}/messages",
        headers={"X-API-Key": "key-alice"},
        json={
            "prompt": "Oversize same filename #2",
            "attachments": [{"url": "https://files.example.test/report.bin?case=oversize_2chunks"}],
        },
    )
    assert r_ov_2.status_code == 400
    monkeypatch.setattr(_att_mod.os, "unlink", real_unlink)
    monkeypatch.setattr(_att_mod.httpx, "AsyncClient", real_async_client)
    monkeypatch.delenv("MAX_ATTACHMENT_BYTES", raising=False)
    monkeypatch.delenv("ATTACHMENT_HOST_ALLOWLIST", raising=False)

    s_ov_2 = http_client.get(f"/v1/sessions/{sid_att}", headers={"X-API-Key": "key-alice"}).json()
    assert s_ov_2["status"] == "idle"
    expected_retained = 43 + 8192 + 16384
    assert s_ov_2["total_attachment_bytes"] == expected_retained

    # 4715616286: httpx.ReadTimeout on 2nd attachment rolls back 1st attachment, finalizes turn as failed, restores idle (502)
    real_sess_mat = _sess_mod.materialize_attachment
    url_seq = {"n": 0}

    async def _second_times_out(spec, workspace_path, **kwargs):
        url_seq["n"] += 1
        if url_seq["n"] == 1:
            return await real_sess_mat(spec, workspace_path, **kwargs)
        raise _httpx.ReadTimeout("Simulated URL stream timeout on second attachment")

    monkeypatch.setattr(_sess_mod, "materialize_attachment", _second_times_out)
    r_url_to = http_client.post(
        f"/v1/sessions/{sid_att}/messages",
        headers={"X-API-Key": "key-alice"},
        json={
            "prompt": "Two attachments where second times out",
            "attachments": [
                {"filename": "ok_first.bin", "content_b64": payload_32},
                {"url": "https://example.com/slow.bin"},
            ],
        },
    )
    assert r_url_to.status_code == 502
    monkeypatch.setattr(_sess_mod, "materialize_attachment", real_sess_mat)
    s_after_to = http_client.get(f"/v1/sessions/{sid_att}", headers={"X-API-Key": "key-alice"}).json()
    assert s_after_to["status"] == "idle"
    assert s_after_to["total_attachment_bytes"] == expected_retained  # first attachment rolled back cleanly

    # 4715131526: Uncertain leftover byte size (rem_bytes is None because stat also failed) retires session as 'failed' (500)
    async def _uncertain_io_err(spec, workspace_path, **kwargs):
        io_e = OSError(errno.EIO, "I/O failure with unreadable leftover")
        setattr(io_e, "_unremoved_attachment_files", [(os.path.join(workspace_path, "attachments", "mystery.bin"), None)])
        raise io_e

    monkeypatch.setattr(_sess_mod, "materialize_attachment", _uncertain_io_err)
    r_uncertain = http_client.post(
        f"/v1/sessions/{sid_att}/messages",
        headers={"X-API-Key": "key-alice"},
        json={
            "prompt": "Uncertain leftover bytes",
            "attachments": [{"filename": "mystery.bin", "content_b64": payload_32}],
        },
    )
    assert r_uncertain.status_code == 500
    monkeypatch.setattr(_sess_mod, "materialize_attachment", real_sess_mat)
    assert http_client.get(f"/v1/sessions/{sid_att}", headers={"X-API-Key": "key-alice"}).json()["status"] == "failed"

    # --- 4715131535 & 4715616318: DB unavailable during atomic commit + fallbacks; stdin.write called EXACTLY once ---
    rate_limiter.history.clear()
    r_db_spawn = http_client.post(
        "/v1/sessions",
        headers={"X-API-Key": "key-alice"},
        json={"agent": "mock_agy_session"},
    )
    assert r_db_spawn.status_code == 201
    sid_db = r_db_spawn.json()["id"]

    live_db_obj = session_manager._sessions[sid_db]
    stdin_writes = []
    orig_stdin_write = live_db_obj.proc.stdin.write
    monkeypatch.setattr(
        live_db_obj.proc.stdin,
        "write",
        lambda payload: (stdin_writes.append(bytes(payload)), orig_stdin_write(payload))[1],
    )

    real_atomic = _sess_mod.complete_turn_and_update_session
    real_finish_turn = _sess_mod.finish_session_turn
    real_close_row = _sess_mod.close_session_row

    def _db_down(*args, **kwargs):
        raise sqlite3.OperationalError("Simulated database outage during turn completion and fallback")

    monkeypatch.setattr(_sess_mod, "complete_turn_and_update_session", _db_down)
    monkeypatch.setattr(_sess_mod, "finish_session_turn", _db_down)
    monkeypatch.setattr(_sess_mod, "close_session_row", _db_down)

    r_db_turn = http_client.post(
        f"/v1/sessions/{sid_db}/messages",
        headers={"X-API-Key": "key-alice"},
        json={"prompt": "Single prompt before DB failure"},
    )
    assert r_db_turn.status_code == 500
    assert not session_manager.is_session_active(sid_db)
    assert len(stdin_writes) == 1

    # While DB writes are still blocked, rows are not yet reconciled
    pre_rec = http_client.get(f"/v1/sessions/{sid_db}?include_turns=true", headers={"X-API-Key": "key-alice"}).json()
    assert pre_rec["status"] == "busy"
    assert len(pre_rec["turns"]) == 1
    assert pre_rec["turns"][0]["status"] == "running"

    # Restore DB access WITHOUT restarting the server; _idle_reaper_loop reconciles both terminal statuses
    monkeypatch.setattr(_sess_mod, "complete_turn_and_update_session", real_atomic)
    monkeypatch.setattr(_sess_mod, "finish_session_turn", real_finish_turn)
    monkeypatch.setattr(_sess_mod, "close_session_row", real_close_row)

    deadline = time.time() + 3.0
    post_rec = None
    while time.time() < deadline:
        cand = http_client.get(f"/v1/sessions/{sid_db}?include_turns=true", headers={"X-API-Key": "key-alice"}).json()
        if cand["status"] == "failed" and cand["turns"] and cand["turns"][0]["status"] == "failed":
            post_rec = cand
            break
        time.sleep(0.1)

    assert post_rec is not None, f"Expected live DB reconciliation to failed; got {cand}"
    assert len(stdin_writes) == 1  # Prompt was sent to subprocess stdin once and never resent during reconciliation
    assert len(post_rec["turns"]) == 1
    assert post_rec["turn_count"] == 0

    # --- 4715616282: Initial 'busy' write failure enters DB recovery without sending prompt (stdin_writes == 0) ---
    r_busy_spawn = http_client.post(
        "/v1/sessions",
        headers={"X-API-Key": "key-alice"},
        json={"agent": "mock_agy_session"},
    )
    sid_busy = r_busy_spawn.json()["id"]
    live_busy = session_manager._sessions[sid_busy]
    busy_writes = []
    orig_bw = live_busy.proc.stdin.write
    monkeypatch.setattr(live_busy.proc.stdin, "write", lambda p: (busy_writes.append(p), orig_bw(p))[1])

    real_upd_state = _sess_mod.update_session_state
    monkeypatch.setattr(_sess_mod, "update_session_state", _db_down)
    monkeypatch.setattr(_sess_mod, "finish_session_turn", _db_down)
    monkeypatch.setattr(_sess_mod, "close_session_row", _db_down)

    r_busy_fail = http_client.post(
        f"/v1/sessions/{sid_busy}/messages",
        headers={"X-API-Key": "key-alice"},
        json={"prompt": "Must never reach stdin"},
    )
    assert r_busy_fail.status_code == 500
    assert len(busy_writes) == 0
    monkeypatch.setattr(_sess_mod, "update_session_state", real_upd_state)
    monkeypatch.setattr(_sess_mod, "finish_session_turn", real_finish_turn)
    monkeypatch.setattr(_sess_mod, "close_session_row", real_close_row)
    session_manager.reconcile_pending_db_writes()
    rec_busy = http_client.get(f"/v1/sessions/{sid_busy}?include_turns=true", headers={"X-API-Key": "key-alice"}).json()
    assert rec_busy["status"] == "failed"
    assert rec_busy["turns"][0]["status"] == "failed"
    assert len(busy_writes) == 0

    # --- 4715616280, 4715616277, 4715616271, 4715616305: Winning 'closed' preserved, pair check, job DEFAULT_EFFORT, colon gate ---
    from app.agents import build_agy_argv, build_claude_argv
    from app.db import close_session_row as _db_close_row, complete_turn_and_update_session as _db_complete, create_session_turn as _db_create_turn

    monkeypatch.setenv("DEFAULT_EFFORT", "low")
    agy_cmd = build_agy_argv("hello", model="gemini-3.8-flash-high", effort=None)
    assert "--effort" not in agy_cmd
    assert "gemini-3.8-flash-high" in agy_cmd
    claude_cmd = build_claude_argv("hello", effort=None)
    assert "--effort" not in claude_cmd
    monkeypatch.delenv("DEFAULT_EFFORT", raising=False)

    with pytest.raises(LookupError):
        _db_complete(turn_id="nonexistent-turn", turn_status="completed", session_id=sid_busy, turn_count=99)

    r_win = http_client.post("/v1/sessions", headers={"X-API-Key": "key-alice"}, json={"agent": "mock_agy_session"})
    sid_win = r_win.json()["id"]
    t_win = _db_create_turn(session_id=sid_win, turn_index=0, prompt="p")
    session_manager._pending_db_reconcile[sid_win] = (t_win["id"], "failed", "stale failure intent", None)
    http_client.delete(f"/v1/sessions/{sid_win}", headers={"X-API-Key": "key-alice"})
    session_manager.reconcile_pending_db_writes()
    win_doc = http_client.get(f"/v1/sessions/{sid_win}?include_turns=true", headers={"X-API-Key": "key-alice"}).json()
    assert win_doc["status"] == "closed"  # Winning 'closed' status was NOT overwritten by 'failed'
    assert win_doc["turns"][0]["status"] == "failed"  # Pending turn still reached terminal 'failed'
