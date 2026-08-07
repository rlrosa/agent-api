import asyncio
import logging
import time
from typing import Any, Dict, List, Optional
from app.config import get_settings
from app.db import claim_next_job, finish_job, list_jobs, reset_orphans, schedule_retry
from app.ratelimit import is_rate_limit_error, rate_limit_manager
from app.runner import run_job


logger = logging.getLogger("agent-api.dispatcher")

_waiters: Dict[str, List[asyncio.Future]] = {}
_waiters_lock = asyncio.Lock()
_wake_event: Optional[asyncio.Event] = None
_worker_tasks: List[asyncio.Task] = []


def get_wake_event() -> asyncio.Event:
    global _wake_event
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if _wake_event is None or (_wake_event._loop != loop if loop else False):
        _wake_event = asyncio.Event()
    return _wake_event



def wake_dispatcher() -> None:
    event = get_wake_event()
    event.set()


async def register_waiter(job_id: str) -> asyncio.Future:
    async with _waiters_lock:
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        if job_id not in _waiters:
            _waiters[job_id] = []
        _waiters[job_id].append(fut)
        return fut


async def unregister_waiter(job_id: str, fut: asyncio.Future) -> None:
    async with _waiters_lock:
        if job_id in _waiters:
            if fut in _waiters[job_id]:
                _waiters[job_id].remove(fut)
            if not _waiters[job_id]:
                del _waiters[job_id]


async def resolve_waiters(job_id: str, result: Dict[str, Any]) -> None:
    async with _waiters_lock:
        futures = _waiters.pop(job_id, [])
        for fut in futures:
            if not fut.done():
                fut.set_result(result)


async def _worker_loop(worker_id: int) -> None:
    logger.info(f"Worker {worker_id} started")
    event = get_wake_event()
    settings = get_settings()

    while True:
        try:
            # 1. Global Cooldown Check
            if rate_limit_manager.is_in_cooldown():
                rem = rate_limit_manager.get_cooldown_remaining()
                await asyncio.sleep(min(1.0, rem))
                continue

            # 2. AIMD Effective Concurrency Check
            running_jobs = list_jobs(status="running", db_path=settings.db_path)
            if len(running_jobs) >= rate_limit_manager.effective_concurrency:
                await asyncio.sleep(0.2)
                continue

            # 3. Atomic Claim
            job = claim_next_job(db_path=settings.db_path)
            if job:
                logger.info(f"Worker {worker_id} claimed job {job['id']} (agent={job['agent']}, attempt={job['attempts']})")
                try:
                    res = await run_job(job)
                    stdout = res.get("stdout") if res else None
                    stderr = res.get("stderr") if res else None
                    error = res.get("error") if res else None
                    exit_code = res.get("exit_code") if res else None

                    if is_rate_limit_error(stdout=stdout, stderr=stderr, error=error, exit_code=exit_code):
                        attempts = job.get("attempts", 1)
                        if attempts < settings.max_attempts:
                            delay = await rate_limit_manager.handle_rate_limit(attempts)
                            next_attempt = time.time() + delay
                            schedule_retry(job["id"], next_attempt, db_path=settings.db_path)
                            logger.warning(
                                f"Timestamp {time.time():.3f}: Job {job['id']} rate-limited on attempt {attempts}. "
                                f"Scheduled retry at {next_attempt:.3f} (+{delay:.2f}s delay)"
                            )
                        else:
                            fail_err = f"Rate limited after {attempts} attempts"
                            res_failed = finish_job(
                                job["id"],
                                status="failed",
                                error=fail_err,
                                db_path=settings.db_path,
                            )
                            logger.error(f"Job {job['id']} failed: {fail_err}")
                            await resolve_waiters(job["id"], res_failed or res)
                    else:
                        if res and res.get("status") == "completed":
                            await rate_limit_manager.handle_success()
                        await resolve_waiters(job["id"], res)
                except Exception as exc:
                    logger.error(f"Worker {worker_id} error executing job {job['id']}: {exc}", exc_info=True)
            else:
                try:
                    await asyncio.wait_for(event.wait(), timeout=1.0)
                    event.clear()
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            logger.info(f"Worker {worker_id} cancelled")
            break
        except Exception as exc:
            logger.error(f"Unexpected error in worker {worker_id} loop: {exc}", exc_info=True)
            await asyncio.sleep(0.5)



async def start_dispatcher() -> None:
    global _worker_tasks, _wake_event
    _wake_event = None
    settings = get_settings()

    # Reset orphaned running jobs from previous process run
    reset_count = reset_orphans()
    if reset_count > 0:
        logger.info(f"Reset {reset_count} orphaned running jobs to pending")

    concurrency = settings.max_concurrency
    _worker_tasks = [
        asyncio.create_task(_worker_loop(i)) for i in range(concurrency)
    ]
    logger.info(f"Started dispatcher with {concurrency} workers")


async def stop_dispatcher() -> None:
    global _worker_tasks, _wake_event
    _wake_event = None
    for t in _worker_tasks:
        t.cancel()
    if _worker_tasks:
        await asyncio.gather(*_worker_tasks, return_exceptions=True)
    _worker_tasks = []
    logger.info("Stopped dispatcher")

