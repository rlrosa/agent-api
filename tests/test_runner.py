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


@pytest.mark.asyncio
async def test_sweep_orphaned_workspaces(tmp_env):
    from app.runner import sweep_orphaned_workspaces
    from app.db import finish_job


    job1 = create_job("agy", "Completed job", db_path=tmp_env["db_path"])
    job1_id = job1["id"]
    claimed1 = claim_next_job(db_path=tmp_env["db_path"])
    finish_job(job1_id, status="completed", db_path=tmp_env["db_path"])

    ws1 = os.path.join(tmp_env["work_root"], job1_id)
    os.makedirs(ws1, exist_ok=True)
    old_time = time.time() - 600
    os.utime(ws1, (old_time, old_time))

    job2 = create_job("agy", "Retry job", db_path=tmp_env["db_path"])
    job2_id = job2["id"]
    claimed2 = claim_next_job(db_path=tmp_env["db_path"])
    finish_job(job2_id, status="pending_retry", db_path=tmp_env["db_path"])

    ws2 = os.path.join(tmp_env["work_root"], job2_id)
    os.makedirs(ws2, exist_ok=True)
    os.utime(ws2, (old_time, old_time))

    job3 = create_job("agy", "Recent job", db_path=tmp_env["db_path"])
    job3_id = job3["id"]
    claimed3 = claim_next_job(db_path=tmp_env["db_path"])
    finish_job(job3_id, status="completed", db_path=tmp_env["db_path"])

    ws3 = os.path.join(tmp_env["work_root"], job3_id)
    os.makedirs(ws3, exist_ok=True)

    reaped = sweep_orphaned_workspaces(
        work_root=tmp_env["work_root"],
        db_path=tmp_env["db_path"],
        min_age_seconds=300.0,
    )

    assert reaped == 1
    assert not os.path.exists(ws1), "Completed old workspace must be reaped"
    assert os.path.exists(ws2), "Pending retry workspace must be preserved"
    assert os.path.exists(ws3), "Recent workspace must be preserved"


def test_rate_limit_exit0_with_stderr_warning_is_not_rate_limited():
    from app.ratelimit import is_rate_limit_error

    # Case 1: Process succeeded (exit 0) with valid extraction JSON in stdout, but stderr has rate limit noise
    stdout_json = '{"is_receipt": true, "payee": "Test Payee S.R.L.", "amount": 4830, "currency": "$UY"}'
    stderr_warning = "HTTP 429: Too Many Requests encountered during subagent execution; retrying..."

    is_rl = is_rate_limit_error(
        stdout=stdout_json,
        stderr=stderr_warning,
        error=None,
        exit_code=0,
    )
    assert is_rl is False, "Exit code 0 with usable stdout MUST NOT be classified as rate-limited error"

    # Case 2: Process failed (exit 1) with rate limit noise in stderr -> SHOULD be classified as rate-limited
    is_rl_failed = is_rate_limit_error(
        stdout="",
        stderr=stderr_warning,
        error="Process exited with exit code 1",
        exit_code=1,
    )
    assert is_rl_failed is True, "Failed exit code with rate limit stderr MUST be classified as rate-limited error"


def test_av_italia_4429_ocr_address_is_not_rate_limited():
    from app.ratelimit import is_rate_limit_error

    # Alad Alfombras receipt containing street address "AV ITALIA 4429" inside stdout envelope
    stdout_alad_envelope = json.dumps({
        "status": "SUCCESS",
        "response": '{"is_receipt": true, "payee": "Alad Alfombras S.R.L.", "address": "AV ITALIA 4429", "amount": 4830, "currency": "$UY"}'
    })

    # Test Case A: Exit 0 run
    assert is_rate_limit_error(stdout=stdout_alad_envelope, stderr="", exit_code=0) is False

    # Test Case B: Non-zero exit code with SUCCESS envelope status
    assert is_rate_limit_error(stdout=stdout_alad_envelope, stderr="Warning notice", exit_code=1) is False


def test_price_429_in_response_payload_is_never_rate_limited():
    from app.ratelimit import is_rate_limit_error

    # Receipt with item price $ 429,00 or VERDULERIA 429 inside response payload
    stdout_price_429 = json.dumps({
        "status": "SUCCESS",
        "response": "TOTAL $ 429,00\nVERDULERIA 429"
    })

    # Test Case 1: Exit 0 run
    assert is_rate_limit_error(stdout=stdout_price_429, stderr="", exit_code=0) is False

    # Test Case 2: Exit 1 run (response payload must be excluded from pattern scanning)
    assert is_rate_limit_error(stdout=stdout_price_429, stderr="Warning: step took 4s", exit_code=1) is False


def test_genuine_rate_limit_is_detected():
    from app.ratelimit import is_rate_limit_error

    # Genuine rate limit in stderr
    assert is_rate_limit_error(stdout="", stderr="HTTP 429: Too Many Requests", exit_code=1) is True

    # Genuine rate limit in envelope error metadata
    stdout_err_envelope = json.dumps({
        "status": "ERROR",
        "error": "RESOURCE_EXHAUSTED: Rate limit exceeded for model"
    })
    assert is_rate_limit_error(stdout=stdout_err_envelope, stderr="", exit_code=1) is True


def test_unparseable_stdout_falls_back_safely():
    from app.ratelimit import is_rate_limit_error

    # Unparseable CLI crash output with non-rate-limit error
    assert is_rate_limit_error(stdout="Fatal crash: Segmentation fault (core dumped)", stderr="Process aborted", exit_code=139) is False

    # Unparseable CLI crash output with genuine rate limit in stderr
    assert is_rate_limit_error(stdout="Fatal crash: Segmentation fault", stderr="Error: 429 Too Many Requests", exit_code=1) is True






