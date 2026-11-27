import json
import os
import stat
import pytest

from app.config import get_settings
from app.db import claim_next_job, create_job, init_db
from app.runner import check_bwrap_confinement, run_job, wrap_cmd_with_bwrap
from app.sandbox import (
    BwrapBackend,
    OpenShellBackend,
    check_confinement,
    get_sandbox_backend,
    sanitize_sandbox_name,
    strip_openshell_banner,
)


@pytest.fixture
def sandbox_env(tmp_path, monkeypatch):
    db_file = str(tmp_path / "sandbox_test.db")
    work_dir = str(tmp_path / "jobs")
    fake_home = tmp_path / "home"
    gemini_cli = fake_home / ".gemini" / "antigravity-cli"
    gemini_cfg = fake_home / ".gemini" / "config"
    claude_dir = fake_home / ".claude"
    local_bin = fake_home / ".local" / "bin"

    gemini_cli.mkdir(parents=True, exist_ok=True)
    gemini_cfg.mkdir(parents=True, exist_ok=True)
    claude_dir.mkdir(parents=True, exist_ok=True)
    local_bin.mkdir(parents=True, exist_ok=True)

    # Create allowed OAuth & config files
    (gemini_cli / "antigravity-oauth-token").write_text('{"refresh_token":"fake-oauth"}')
    (gemini_cli / "settings.json").write_text('{"theme":"dark"}')
    (gemini_cli / "installation_id").write_text("fake-install-id")
    (claude_dir / ".credentials.json").write_text('{"claudeAiOauth":{"accessToken":"fake"}}')
    (claude_dir / "settings.json").write_text("{}")
    (fake_home / ".claude.json").write_text("{}")

    # Create sensitive host files that MUST NEVER be exposed inside the sandbox (F5)
    (gemini_cli / "history.jsonl").write_text('{"secret_prompt":"DO_NOT_EXPOSE_HISTORY"}')
    (gemini_cli / "conversation_summaries.db").write_text("SQLITE_SECRET_SUMMARIES")
    (gemini_cli / "jetbox_summaries_proto.pb").write_text("PROTO_SECRET")
    (claude_dir / "history.jsonl").write_text('{"claude_history":"DO_NOT_EXPOSE"}')

    monkeypatch.setenv("API_KEY", "test-secret-key")
    monkeypatch.setenv("DB_PATH", db_file)
    monkeypatch.setenv("WORK_ROOT", work_dir)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("BWRAP_ENABLED", "1")
    monkeypatch.setenv("ALLOW_UNCONFINED", "0")
    monkeypatch.setenv("SANDBOX_BACKEND", "bwrap")
    monkeypatch.setenv("EGRESS_RESTRICT", "0")

    init_db(db_file)
    return {
        "db_path": db_file,
        "work_root": work_dir,
        "home": str(fake_home),
        "gemini_cli": str(gemini_cli),
        "claude_dir": str(claude_dir),
    }


def test_bwrap_f5_allowlist_mounts_exclude_history_and_db(sandbox_env):
    ws = os.path.join(sandbox_env["work_root"], "job-f5")
    os.makedirs(ws, exist_ok=True)

    argv = wrap_cmd_with_bwrap(["agy", "-p", "hi"], ws, "agy", job_id="job-f5")
    joined = " ".join(argv)

    # 1. Must mount --tmpfs over ~/.gemini, NOT --ro-bind the whole ~/.gemini directory
    gemini_root = os.path.join(sandbox_env["home"], ".gemini")
    for idx, token in enumerate(argv[:-2]):
        if token == "--ro-bind":
            assert argv[idx + 1] != gemini_root, "Whole ~/.gemini directory must never be --ro-bind mounted"

    # 2. Allowed OAuth token and settings are mounted
    assert os.path.join(sandbox_env["gemini_cli"], "antigravity-oauth-token") in argv
    assert os.path.join(sandbox_env["gemini_cli"], "settings.json") in argv
    assert os.path.join(sandbox_env["gemini_cli"], "installation_id") in argv

    # 3. Sensitive files are NEVER in argv
    assert "history.jsonl" not in joined
    assert "conversation_summaries.db" not in joined
    assert "jetbox_summaries_proto.pb" not in joined


@pytest.mark.asyncio
async def test_bwrap_f5_runtime_history_is_unreadable(sandbox_env):
    job = create_job("agy", "Check F5 file visibility", db_path=sandbox_env["db_path"])
    claimed = claim_next_job(db_path=sandbox_env["db_path"])

    probe_script = (
        "import os, json; "
        f"cli = {json.dumps(sandbox_env['gemini_cli'])}; "
        "res = {"
        "'token_exists': os.path.exists(os.path.join(cli, 'antigravity-oauth-token')), "
        "'history_exists': os.path.exists(os.path.join(cli, 'history.jsonl')), "
        "'summaries_exists': os.path.exists(os.path.join(cli, 'conversation_summaries.db')), "
        "'proto_exists': os.path.exists(os.path.join(cli, 'jetbox_summaries_proto.pb')), "
        "'brain_writable': os.access(os.path.join(cli, 'brain'), os.W_OK)"
        "}; "
        "print(json.dumps(res))"
    )
    result = await run_job(claimed, custom_argv=["python3", "-c", probe_script])
    assert result["status"] == "completed", f"Unexpected failure: {result}"
    data = json.loads(result["stdout"].strip())
    assert data["token_exists"] is True
    assert data["brain_writable"] is True
    assert data["history_exists"] is False, "history.jsonl leaked into bwrap sandbox!"
    assert data["summaries_exists"] is False, "conversation_summaries.db leaked into bwrap sandbox!"
    assert data["proto_exists"] is False, "jetbox_summaries_proto.pb leaked into bwrap sandbox!"


def test_bwrap_f6_egress_restrict_unshare_net(sandbox_env, monkeypatch):
    ws = os.path.join(sandbox_env["work_root"], "job-f6")
    os.makedirs(ws, exist_ok=True)

    # Default EGRESS_RESTRICT=0 -> --share-net
    argv_open = wrap_cmd_with_bwrap(["agy", "-p", "hi"], ws, "agy")
    assert "--share-net" in argv_open
    assert "--unshare-net" not in argv_open

    # EGRESS_RESTRICT=1 -> --unshare-net
    monkeypatch.setenv("EGRESS_RESTRICT", "1")
    argv_restricted = wrap_cmd_with_bwrap(["agy", "-p", "hi"], ws, "agy")
    assert "--unshare-net" in argv_restricted
    assert "--share-net" not in argv_restricted


def test_openshell_backend_wrap_and_fail_closed(sandbox_env, monkeypatch, tmp_path):
    monkeypatch.setenv("SANDBOX_BACKEND", "openshell")
    monkeypatch.setenv("OPENSHELL_IMAGE", "agent-api-sandbox:test")
    monkeypatch.setenv("OPENSHELL_POLICY_PATH", "./policies/openshell-policy.yaml")

    ws = os.path.join(sandbox_env["work_root"], "job-os-123")
    os.makedirs(ws, exist_ok=True)

    # 1. Fail-closed when openshell binary is absent and ALLOW_UNCONFINED=0
    monkeypatch.setenv("PATH", "/nonexistent")
    conf_missing = check_bwrap_confinement()
    assert conf_missing == {
        "enabled": True,
        "available": False,
        "backend": "openshell",
        "mode": "enforced",
    }
    with pytest.raises(RuntimeError, match="openshell binary missing"):
        wrap_cmd_with_bwrap(["agy", "-p", "hello"], ws, "agy", job_id="job-os-123")

    # 2. Escape hatch when ALLOW_UNCONFINED=1
    monkeypatch.setenv("ALLOW_UNCONFINED", "1")
    unconfined_argv = wrap_cmd_with_bwrap(["agy", "-p", "hello"], ws, "agy", job_id="job-os-123")
    assert unconfined_argv == ["agy", "-p", "hello"]
    monkeypatch.setenv("ALLOW_UNCONFINED", "0")

    # 3. With openshell binary on PATH -> wraps with policy & allowlist mounts
    bin_dir = tmp_path / "fake_bin"
    bin_dir.mkdir(exist_ok=True)
    fake_openshell = bin_dir / "openshell"
    fake_openshell.write_text("#!/bin/sh\nexit 0\n")
    fake_openshell.chmod(fake_openshell.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")

    conf_ready = check_bwrap_confinement()
    assert conf_ready == {
        "enabled": True,
        "available": True,
        "backend": "openshell",
        "mode": "enforced",
    }

    wrapped = wrap_cmd_with_bwrap(["agy", "-p", "hello"], ws, "agy", job_id="job-os-123")
    assert wrapped[0] == str(fake_openshell)
    assert wrapped[1:3] == ["sandbox", "create"]
    assert wrapped[wrapped.index("--name") + 1] == "aapi-job-os-123"
    assert len(wrapped[wrapped.index("--name") + 1]) <= 19
    assert "--no-tty" in wrapped
    assert wrapped[wrapped.index("--from") + 1] == "agent-api-sandbox:test"
    assert wrapped[wrapped.index("--policy") + 1].endswith("policies/openshell-policy.yaml")

    # Verify full UUID job_id also respects OpenShell's 19-char max sandbox name limit
    uuid_name = sanitize_sandbox_name("e0bc8888-7606-442d-93bd-a2ee5ca9da12", ws)
    assert len(uuid_name) == 19
    assert uuid_name.startswith("aapi-e0bc8888-")

    # Verify banner stripping preserves pure JSON payload
    noisy_stdout = (
        "Created sandbox: aapi-job-os-123\n"
        "⠁ Requesting sandbox... (0s)\r✓ Sandbox allocated (0s)\n"
        "[0.0s] Pulling image agent-api-sandbox:test\n"
        "[0.3s] Starting sandbox Container created\n"
        '{"status":"SUCCESS","response":"clean answer"}\n'
        "✓ Deleted sandbox aapi-job-os-123\n"
    )
    assert strip_openshell_banner(noisy_stdout) == '{"status":"SUCCESS","response":"clean answer"}'

    driver_cfg_raw = wrapped[wrapped.index("--driver-config-json") + 1]
    driver_cfg = json.loads(driver_cfg_raw)
    mounts = driver_cfg["docker"]["mounts"]

    mount_sources = [m["source"] for m in mounts]
    assert os.path.abspath(ws) in mount_sources
    assert os.path.join(sandbox_env["gemini_cli"], "antigravity-oauth-token") in mount_sources
    assert not any("history.jsonl" in s for s in mount_sources)
    assert not any("conversation_summaries.db" in s for s in mount_sources)

    sep_idx = wrapped.index("--")
    assert wrapped[sep_idx + 1 :] == ["agy", "-p", "hello"]


@pytest.mark.asyncio
async def test_openshell_cleanup_on_finish_and_timeout(sandbox_env, monkeypatch, tmp_path):
    bin_dir = tmp_path / "fake_os_bin"
    bin_dir.mkdir(exist_ok=True)
    log_file = tmp_path / "openshell_calls.log"
    fake_openshell = bin_dir / "openshell"

    # Script logs every invocation ('sandbox create ...' and 'sandbox delete ...')
    # and on 'sandbox create', executes the command after '--'
    fake_openshell.write_text(
        "#!/usr/bin/env bash\n"
        f"echo \"$*\" >> {log_file}\n"
        "if [ \"$1\" = \"sandbox\" ] && [ \"$2\" = \"create\" ]; then\n"
        "  while [ $# -gt 0 ]; do\n"
        "    if [ \"$1\" = \"--\" ]; then shift; exec \"$@\"; fi\n"
        "    shift\n"
        "  done\n"
        "fi\n"
        "exit 0\n"
    )
    fake_openshell.chmod(fake_openshell.stat().st_mode | stat.S_IEXEC)

    monkeypatch.setenv("SANDBOX_BACKEND", "openshell")
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")

    # 1. Normal completion triggers sandbox delete
    job1 = create_job("agy", "OpenShell success test", db_path=sandbox_env["db_path"])
    claimed1 = claim_next_job(db_path=sandbox_env["db_path"])
    res1 = await run_job(claimed1, custom_argv=["python3", "-c", "print('ok from openshell')"])
    assert res1["status"] == "completed"
    assert "ok from openshell" in res1["stdout"]

    calls = log_file.read_text().splitlines()
    expected_name1 = sanitize_sandbox_name(job1["id"], "")
    assert any(f"sandbox create --name {expected_name1}" in c for c in calls)
    assert any(f"sandbox delete {expected_name1}" in c for c in calls)

    # 2. Timeout also triggers sandbox delete in finally block
    job2 = create_job("agy", "OpenShell timeout test", timeout=1, db_path=sandbox_env["db_path"])
    claimed2 = claim_next_job(db_path=sandbox_env["db_path"])
    res2 = await run_job(claimed2, custom_argv=["python3", "-c", "import time; time.sleep(30)"])
    assert res2["status"] == "failed"
    assert "timed out" in res2["error"]

    calls_after = log_file.read_text().splitlines()
    expected_name2 = sanitize_sandbox_name(job2["id"], "")
    assert any(f"sandbox delete {expected_name2}" in c for c in calls_after)
