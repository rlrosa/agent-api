import json
import time
import pytest
from app.ratelimit import parse_reset_hint
from app.db import create_job, schedule_retry, claim_next_job, get_job, init_db


def test_parse_reset_hint():
    # Various formats
    assert parse_reset_hint("Resets in 1h45m23s") == 6323.0
    assert parse_reset_hint("2h20m35s") == 8435.0
    assert parse_reset_hint("45m") == 2700.0
    assert parse_reset_hint("90s") == 90.0
    assert parse_reset_hint("Resets in 2h20m35s") == 8435.0
    assert parse_reset_hint("Resets in 45m") == 2700.0
    assert parse_reset_hint("Resets in 90s") == 90.0

    # Real error format from production jobs.db
    real_stderr = (
        "error: Individual quota reached. Please upgrade your subscription to increase your limits. Resets in 1h47m43s.\n"
        "AGY_ERROR: {\"short_error\":\"RESOURCE_EXHAUSTED (code 429): Individual quota reached. Resets in 1h47m43s.\"}"
    )
    assert parse_reset_hint(real_stderr) == 6463.0

    # No hint
    assert parse_reset_hint("Task failed with internal server error") is None
    assert parse_reset_hint("") is None
    assert parse_reset_hint(None) is None


def test_schedule_retry_reset_attempts(tmp_path):
    db_path = str(tmp_path / "test_jobs.db")
    init_db(db_path)
    job = create_job("agy", "test prompt", db_path=db_path)
    job_id = job["id"]

    # Claim job multiple times to simulate attempts reaching max
    for _ in range(4):
        claimed = claim_next_job(now=time.time() + 1000, db_path=db_path)
        assert claimed is not None
        # Update next_attempt_at so it can be claimed again if pending
        schedule_retry(job_id, next_attempt_at=time.time() - 10, db_path=db_path)

    claimed = claim_next_job(now=time.time() + 1000, db_path=db_path)
    assert claimed["attempts"] == 5

    # Now schedule retry with reset_attempts=True
    meta = {"quota_requeued": True}
    now = time.time()
    requeued = schedule_retry(
        job_id,
        next_attempt_at=now,
        db_path=db_path,
        reset_attempts=True,
        metadata=meta,
    )
    assert requeued["status"] == "pending"
    assert requeued["attempts"] == 0
    assert requeued["metadata"]["quota_requeued"] is True

    # Check DB state directly
    fetched = get_job(job_id, db_path=db_path)
    assert fetched["status"] == "pending"
    assert fetched["attempts"] == 0

    # Claim next job: attempts must increment from 0 to 1
    next_claim = claim_next_job(now=now + 10, db_path=db_path)
    assert next_claim is not None
    assert next_claim["id"] == job_id
    assert next_claim["status"] == "running"
    assert next_claim["attempts"] == 1


def test_quota_requeue_guards(tmp_path):
    db_path = str(tmp_path / "test_jobs.db")
    init_db(db_path)
    job = create_job("agy", "test prompt", db_path=db_path)
    job_id = job["id"]

    # Test that a hint > 3h returns > 10800
    big_hint = parse_reset_hint("Resets in 3h30m")
    assert big_hint == 3.5 * 3600
    assert big_hint > 3 * 3600

    # Test metadata tracking
    job = get_job(job_id, db_path=db_path)
    meta = job.get("metadata") or {}
    assert not meta.get("quota_requeued")

    meta["quota_requeued"] = True
    schedule_retry(job_id, time.time(), db_path=db_path, reset_attempts=True, metadata=meta)

    job_after = get_job(job_id, db_path=db_path)
    assert job_after["metadata"]["quota_requeued"] is True

