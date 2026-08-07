import os
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
import pytest

from app.db import (
    cancel_job,
    claim_next_job,
    create_job,
    finish_job,
    get_job,
    init_db,
    list_jobs,
    purge_old,
    reset_orphans,
    schedule_retry,
)


@pytest.fixture
def tmp_db_path(tmp_path):
    db_file = str(tmp_path / "test_jobs.db")
    os.environ["API_KEY"] = "testkey"
    init_db(db_file)
    return db_file


def test_init_and_create_job(tmp_db_path):
    job = create_job(
        agent="agy",
        prompt="Hello world",
        model="gemini-3.6-flash-low",
        metadata={"caller": "unit-test"},
        wait=60,
        timeout=120,
        db_path=tmp_db_path,
    )
    assert job is not None
    assert job["agent"] == "agy"
    assert job["prompt"] == "Hello world"
    assert job["model"] == "gemini-3.6-flash-low"
    assert job["status"] == "pending"
    assert job["attempts"] == 0
    assert job["metadata"] == {"caller": "unit-test"}
    assert job["wait"] == 60
    assert job["timeout"] == 120
    assert isinstance(job["created_at"], float)


def test_claim_next_job(tmp_db_path):
    job1 = create_job("agy", "Prompt 1", db_path=tmp_db_path)
    time.sleep(0.01)
    job2 = create_job("claude", "Prompt 2", db_path=tmp_db_path)

    now = time.time()
    claimed = claim_next_job(now=now, db_path=tmp_db_path)
    assert claimed is not None
    assert claimed["id"] == job1["id"]
    assert claimed["status"] == "running"
    assert claimed["attempts"] == 1
    assert claimed["started_at"] is not None

    claimed2 = claim_next_job(now=now, db_path=tmp_db_path)
    assert claimed2 is not None
    assert claimed2["id"] == job2["id"]

    no_more = claim_next_job(now=now, db_path=tmp_db_path)
    assert no_more is None


def test_finish_job(tmp_db_path):
    job = create_job("agy", "Test finish", db_path=tmp_db_path)
    claimed = claim_next_job(db_path=tmp_db_path)

    finished = finish_job(
        claimed["id"],
        status="completed",
        exit_code=0,
        stdout="pong\n",
        stderr="",
        db_path=tmp_db_path,
    )
    assert finished["status"] == "completed"
    assert finished["exit_code"] == 0
    assert finished["stdout"] == "pong\n"
    assert finished["finished_at"] is not None


def test_schedule_retry(tmp_db_path):
    job = create_job("agy", "Test retry", db_path=tmp_db_path)
    claimed = claim_next_job(db_path=tmp_db_path)

    future_time = time.time() + 100.0
    retried = schedule_retry(claimed["id"], next_attempt_at=future_time, db_path=tmp_db_path)
    assert retried["status"] == "pending"
    assert retried["next_attempt_at"] == future_time

    # Attempting to claim with current time should return None
    claimed_early = claim_next_job(now=time.time(), db_path=tmp_db_path)
    assert claimed_early is None

    # Claiming with time past future_time should return the job
    claimed_late = claim_next_job(now=future_time + 1.0, db_path=tmp_db_path)
    assert claimed_late is not None
    assert claimed_late["id"] == job["id"]
    assert claimed_late["attempts"] == 2


def test_cancel_job(tmp_db_path):
    job = create_job("agy", "Test cancel", db_path=tmp_db_path)
    canceled = cancel_job(job["id"], db_path=tmp_db_path)
    assert canceled["status"] == "canceled"
    assert canceled["finished_at"] is not None

    # Cannot claim canceled job
    assert claim_next_job(db_path=tmp_db_path) is None


def test_get_and_list_jobs(tmp_db_path):
    job1 = create_job("agy", "P1", db_path=tmp_db_path)
    job2 = create_job("claude", "P2", db_path=tmp_db_path)

    fetched1 = get_job(job1["id"], db_path=tmp_db_path)
    assert fetched1["id"] == job1["id"]

    all_jobs = list_jobs(db_path=tmp_db_path)
    assert len(all_jobs) == 2

    pending_jobs = list_jobs(status="pending", db_path=tmp_db_path)
    assert len(pending_jobs) == 2

    finish_job(job1["id"], status="completed", db_path=tmp_db_path)

    pending_jobs_after = list_jobs(status="pending", db_path=tmp_db_path)
    assert len(pending_jobs_after) == 1
    assert pending_jobs_after[0]["id"] == job2["id"]


def test_reset_orphans(tmp_db_path):
    job1 = create_job("agy", "P1", db_path=tmp_db_path)
    job2 = create_job("claude", "P2", db_path=tmp_db_path)

    # Claim both so they are running
    c1 = claim_next_job(db_path=tmp_db_path)
    c2 = claim_next_job(db_path=tmp_db_path)
    assert c1["status"] == "running"
    assert c2["status"] == "running"
    assert claim_next_job(db_path=tmp_db_path) is None

    # Reset orphans
    reset_count = reset_orphans(db_path=tmp_db_path)
    assert reset_count == 2

    # Verify they can be claimed again
    re_claimed1 = claim_next_job(db_path=tmp_db_path)
    assert re_claimed1 is not None
    assert re_claimed1["status"] == "running"


def test_purge_old(tmp_db_path):
    old_time = time.time() - 1000.0
    job = create_job("agy", "Old job", db_path=tmp_db_path)
    finish_job(job["id"], status="completed", finished_at=old_time, db_path=tmp_db_path)

    recent_job = create_job("agy", "Recent job", db_path=tmp_db_path)
    finish_job(recent_job["id"], status="completed", finished_at=time.time(), db_path=tmp_db_path)

    purged = purge_old(before_ts=time.time() - 500.0, db_path=tmp_db_path)
    assert purged == 1

    remaining = list_jobs(db_path=tmp_db_path)
    assert len(remaining) == 1
    assert remaining[0]["id"] == recent_job["id"]


def test_concurrent_claim(tmp_db_path):
    NUM_JOBS = 50
    NUM_WORKERS = 20

    # Seed 50 pending jobs
    for i in range(NUM_JOBS):
        create_job("agy", f"Job prompt {i}", db_path=tmp_db_path)

    claimed_results = []

    def worker_func():
        local_claims = []
        for _ in range(10):
            job = claim_next_job(db_path=tmp_db_path)
            if job:
                local_claims.append(job["id"])
            time.sleep(0.001)
        return local_claims

    with ThreadPoolExecutor(max_workers=NUM_WORKERS) as executor:
        futures = [executor.submit(worker_func) for _ in range(NUM_WORKERS)]
        for f in futures:
            claimed_results.extend(f.result())

    # Assertions for thread safety and atomic claim:
    # 1. Total claims recorded equals NUM_JOBS (50)
    assert len(claimed_results) == NUM_JOBS
    # 2. Number of distinct claimed IDs equals total claims (no double claim)
    distinct_ids = set(claimed_results)
    assert len(distinct_ids) == NUM_JOBS
