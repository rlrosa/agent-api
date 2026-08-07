import asyncio
import os
import shutil
import time
import pytest
from app.config import get_settings
from app.db import create_job, claim_next_job, init_db, get_job
from app.runner import run_job, build_scrubbed_env


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
