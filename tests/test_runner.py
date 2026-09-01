import asyncio
import json
import os
import shutil

import time
import pytest
from app.config import get_settings
from app.db import create_job, claim_next_job, init_db, get_job
from app.runner import run_job, build_scrubbed_env
from app.attachments import compose_prompt


@pytest.fixture
def tmp_env(tmp_path):
    db_file = str(tmp_path / "runner_test.db")
    work_dir = str(tmp_path / "jobs")
    os.environ["API_KEY"] = "test-secret-key-123"
    os.environ["CANARY_SECRET"] = "do-not-leak-this-secret"
    os.environ["DB_PATH"] = db_file
    os.environ["WORK_ROOT"] = work_dir
    os.environ["JOB_TIMEOUT"] = "120"
    init_db(db_file)
    return {"db_path": db_file, "work_root": work_dir}


@pytest.mark.asyncio
async def test_run_job_mock_success(tmp_env):
    job = create_job("agy", "Reply with hello", db_path=tmp_env["db_path"])
    claimed = claim_next_job(db_path=tmp_env["db_path"])

    custom_argv = ["python3", "-c", "print('hello from runner')"]
    result = await run_job(claimed, custom_argv=custom_argv)

    assert result["status"] == "completed"
    assert result["exit_code"] == 0
    assert "hello from runner" in result["stdout"]
    assert not os.path.exists(os.path.join(tmp_env["work_root"], job["id"]))


@pytest.mark.asyncio
async def test_env_scrubbing(tmp_env):
    job = create_job("agy", "Print env", db_path=tmp_env["db_path"])
    claimed = claim_next_job(db_path=tmp_env["db_path"])

    custom_argv = [
        "python3",
        "-c",
        "import os; [print(f'{k}={v}') for k, v in os.environ.items()]",
    ]
    result = await run_job(claimed, custom_argv=custom_argv)

    assert result["status"] == "completed"
    env_dump = result["stdout"]

    assert "API_KEY" not in env_dump
    assert "CANARY_SECRET" not in env_dump
    assert "PATH=" in env_dump


@pytest.mark.asyncio
async def test_nonzero_exit_outcome(tmp_env):
    job = create_job("agy", "Failing job", db_path=tmp_env["db_path"])
    claimed = claim_next_job(db_path=tmp_env["db_path"])

    custom_argv = [
        "python3",
        "-c",
        "import sys; sys.stderr.write('fatal error occurred\\n'); sys.exit(42)",
    ]
    result = await run_job(claimed, custom_argv=custom_argv)

    assert result["status"] == "failed"
    assert result["exit_code"] == 42
    assert result["error"] == "fatal error occurred"
    assert result["status"] != "completed"


@pytest.mark.asyncio
async def test_timeout_and_pgroup_kill(tmp_env):
    job = create_job("agy", "Sleep job", timeout=2, db_path=tmp_env["db_path"])
    claimed = claim_next_job(db_path=tmp_env["db_path"])

    # Spawns a background child process that sleeps
    script = (
        "import subprocess, sys, time; "
        "subprocess.Popen(['sleep', '100']); "
        "time.sleep(100)"
    )
    custom_argv = ["python3", "-c", script]

    result = await run_job(claimed, custom_argv=custom_argv)

    assert result["status"] == "failed"
    assert result["exit_code"] == -1
    assert "timed out after 2s" in result["error"]
    assert not os.path.exists(os.path.join(tmp_env["work_root"], job["id"]))


@pytest.mark.asyncio
async def test_keep_workspace_on_failure(tmp_env):
    os.environ["KEEP_WORKSPACE_ON_FAILURE"] = "1"
    try:
        job = create_job("agy", "Failing job keep ws", db_path=tmp_env["db_path"])
        claimed = claim_next_job(db_path=tmp_env["db_path"])

        custom_argv = ["python3", "-c", "import sys; sys.exit(1)"]
        result = await run_job(claimed, custom_argv=custom_argv)

        assert result["status"] == "failed"
        ws_path = os.path.join(tmp_env["work_root"], job["id"])
        assert os.path.exists(ws_path)
    finally:
        os.environ["KEEP_WORKSPACE_ON_FAILURE"] = "0"


@pytest.mark.asyncio
async def test_workspace_survives_retry(tmp_env):
    job = create_job("agy", "Analyze attached file", db_path=tmp_env["db_path"])
    job_id = job["id"]
    claimed = claim_next_job(db_path=tmp_env["db_path"])

    ws_path = os.path.join(tmp_env["work_root"], job_id)
    att_dir = os.path.join(ws_path, "attachments")
    os.makedirs(att_dir, exist_ok=True)
    file_path = os.path.join(att_dir, "receipt.jpg")
    with open(file_path, "wb") as f:
        f.write(b"fake image bytes")

    prompt_with_att = compose_prompt("Analyze attached file", ["receipt.jpg"], workspace_path=ws_path)
    claimed["prompt"] = prompt_with_att

    # Rate-limited attempt 1
    custom_argv = ["python3", "-c", "import sys; sys.stderr.write('HTTP 429 Too Many Requests\\n'); sys.exit(429)"]
    result = await run_job(claimed, custom_argv=custom_argv)

    # Assert workspace and attachment file survive
    assert os.path.exists(file_path), "Attachment file must survive rate-limited retry"


@pytest.mark.asyncio
async def test_pre_execution_attachment_guard(tmp_env):
    job = create_job("agy", "Analyze attached file", db_path=tmp_env["db_path"])
    job_id = job["id"]
    claimed = claim_next_job(db_path=tmp_env["db_path"])

    ws_path = os.path.join(tmp_env["work_root"], job_id)
    missing_file_path = os.path.join(ws_path, "attachments", "non_existent.jpg")

    prompt_with_missing_att = (
        "Analyze this\n\n"
        "Attached files (read them from disk as needed):\n"
        f"- {missing_file_path}"
    )
    claimed["prompt"] = prompt_with_missing_att

    custom_argv = ["python3", "-c", "print('this_should_not_run')"]
    result = await run_job(claimed, custom_argv=custom_argv)

    assert result["status"] == "failed"
    assert result["exit_code"] == -1
    assert "Attachment file missing from workspace prior to execution" in result["error"]
    assert missing_file_path in result["error"]
    assert "this_should_not_run" not in (result.get("stdout") or "")


@pytest.mark.asyncio
async def test_output_validation_guard(tmp_env):
    job = create_job("agy", "Parse receipt", db_path=tmp_env["db_path"])
    claimed = claim_next_job(db_path=tmp_env["db_path"])

    unreadable_output = '{"is_receipt": false, "rejection_reason": "The specified media attachment file could not be found or read from disk."}'
    custom_argv = ["python3", "-c", f"print({json.dumps(unreadable_output)})"]
    result = await run_job(claimed, custom_argv=custom_argv)

    assert result["status"] == "failed"
    assert result["exit_code"] == -1
    assert "Output validation guard failed" in result["error"]
    assert "could not be found or read from disk" in result["error"]


