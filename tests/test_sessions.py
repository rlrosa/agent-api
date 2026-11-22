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
def tmp_db_path(tmp_path):
    db_file = str(tmp_path / "test_sessions.db")
    os.environ["API_KEY"] = "testkey"
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
        "MAX_ACTIVE_SESSIONS",
        "MAX_QUEUED_TURNS_PER_SESSION",
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

    assert raw_settings.max_active_sessions == 10
    assert resolved_settings.max_active_sessions == 10

    assert raw_settings.max_queued_turns_per_session == 3
    assert resolved_settings.max_queued_turns_per_session == 3


def test_session_settings_env_overrides(monkeypatch):
    """Verify all 6 session settings honour environment variable overrides."""
    monkeypatch.setenv("API_KEY", "dev")
    monkeypatch.setenv("SESSION_WORK_ROOT", "/custom/sessions")
    monkeypatch.setenv("SESSION_IDLE_TIMEOUT", "450")
    monkeypatch.setenv("SESSION_IDLE_MAX", "1800")
    monkeypatch.setenv("SESSION_TURN_TIMEOUT", "60")
    monkeypatch.setenv("MAX_ACTIVE_SESSIONS", "25")
    monkeypatch.setenv("MAX_QUEUED_TURNS_PER_SESSION", "7")

    s = get_settings()
    assert s.session_work_root == "/custom/sessions"
    assert s.session_idle_timeout == 450
    assert s.session_idle_max == 1800
    assert s.session_turn_timeout == 60
    assert s.max_active_sessions == 25
    assert s.max_queued_turns_per_session == 7


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

    # Basic call: claude -p --input-format stream-json --output-format stream-json
    argv = build_claude_session_argv()
    assert argv[:6] == ["claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json"]
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


@pytest.fixture
def http_client(tmp_path, monkeypatch):
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
    child_proc = live.proc
    ws_path = live.workspace_path
    assert os.path.exists(ws_path)

    # Fast-forward last_active_at by 10s to trigger reaper immediately
    live.last_active_at = time.time() - 10.0
    time.sleep(1.2)

    # Session is reaped
    assert not session_manager.is_session_active(session_id)
    assert not os.path.exists(ws_path)

    # GET returns expired
    r_get = http_client.get(f"/v1/sessions/{session_id}", headers={"X-API-Key": "key-alice"})
    assert r_get.status_code == 200
    assert r_get.json()["status"] == "expired"

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


def test_fifo_turn_serialization_and_queue_depth_cap(http_client):
    """Test FIFO sequential execution and queue depth rejection (429)."""
    r1 = http_client.post(
        "/v1/sessions",
        headers={"X-API-Key": "key-alice"},
        json={"agent": "mock_agy_session"},
    )
    session_id = r1.json()["id"]

    # 2 concurrent turns execute sequentially
    def send_turn(turn_text):
        return http_client.post(
            f"/v1/sessions/{session_id}/messages",
            headers={"X-API-Key": "key-alice"},
            json={"prompt": turn_text},
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(send_turn, "Msg 1")
        f2 = executor.submit(send_turn, "Msg 2")
        res1 = f1.result()
        res2 = f2.result()

    assert res1.status_code == 200
    assert res2.status_code == 200

    # Max queued turns guard: artificially set queued_waiters to max (3)
    live = session_manager._sessions[session_id]
    live.queued_waiters = 3
    r_over = http_client.post(
        f"/v1/sessions/{session_id}/messages",
        headers={"X-API-Key": "key-alice"},
        json={"prompt": "Rejected overflow"},
    )
    assert r_over.status_code == 429
    assert "Session turn queue full" in r_over.json()["detail"]
    assert r_over.headers.get("Retry-After") == "5"
    live.queued_waiters = 0

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
    monkeypatch.setenv("BWRAP_ENABLED", "1")
    monkeypatch.setenv("ALLOW_UNCONFINED", "0")
    monkeypatch.setattr("shutil.which", lambda cmd: None if cmd == "bwrap" else "/bin/python3")

    with pytest.raises(Exception) as excinfo:
        wrap_cmd_with_bwrap(["echo", "hi"], str(tmp_path), "mock_agy_session")
    assert "Confinement error: bubblewrap (bwrap) binary missing" in str(excinfo.value)


@pytest.mark.asyncio
async def test_agy_stream_driver_nested_wire_format():
    """Finding 1: Verify AgyStreamDriver correctly extracts from nested {"event": "result", "result": {...}} wire format."""
    import asyncio
    import json
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


