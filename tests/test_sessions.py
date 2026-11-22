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
    """Verify reset_orphan_sessions transitions active sessions to closed on startup."""
    s1 = create_session_row(agent="agy", status="starting", db_path=tmp_db_path)
    s2 = create_session_row(agent="agy", status="idle", db_path=tmp_db_path)
    s3 = create_session_row(agent="claude", status="busy", db_path=tmp_db_path)
    s4 = create_session_row(agent="claude", status="closed", db_path=tmp_db_path)

    count = reset_orphan_sessions(db_path=tmp_db_path)
    assert count == 3

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

    # s_recent and s_active survive
    assert get_session(s_recent["id"], db_path=tmp_db_path) is not None
    assert get_session(s_active["id"], db_path=tmp_db_path) is not None
