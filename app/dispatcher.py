import asyncio
import logging
import time
from typing import Any, Dict, List, Optional
from app.config import get_settings
from app.db import build_daily_rollups, claim_next_job, finish_job, list_jobs, purge_old, reset_orphans, schedule_retry, sweep_stuck_running_jobs


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
            sweep_stuck_running_jobs(db_path=settings.db_path)
            now = time.time()
            running_jobs = list_jobs(status="running", db_path=settings.db_path)
            active_running = [
                j for j in running_jobs
                if j.get("started_at") and (now - j["started_at"]) <= ((j.get("timeout") or settings.job_timeout) + 10)
            ]
            if len(active_running) >= rate_limit_manager.effective_concurrency:
                await asyncio.sleep(0.2)
                continue


            # 3. Atomic Claim
            job = claim_next_job(db_path=settings.db_path)
            if job:
                logger.info(f"Job {job['id']} claimed by worker {worker_id} (agent={job['agent']}, attempt={job['attempts']})")
                logger.debug(f"Job {job['id']} claim details: model={job.get('model')}, effort={job.get('effort')}, timeout={job.get('timeout')}")
                try:
                    res = await run_job(job)
                    stdout = res.get("stdout") if res else None
                    stderr = res.get("stderr") if res else None
                    error = res.get("error") if res else None
                    exit_code = res.get("exit_code") if res else None

                    res_status = res.get("status") if res else "failed"
                    duration = (res.get("finished_at") or time.time()) - (job.get("started_at") or time.time())
                    logger.info(
                        f"Job {job['id']} finished with status={res_status} (exit_code={exit_code}, "
                        f"duration={duration:.2f}s, attempts={job.get('attempts')})"
                    )
                    logger.debug(
                        f"Job {job['id']} outputs: stdout_len={len(stdout or '')}, "
                        f"stderr_len={len(stderr or '')}, error={error}"
                    )

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


_retention_task: Optional[asyncio.Task] = None


async def _retention_loop() -> None:
    logger.info("Started retention sweeper loop")
    while True:
        try:
            settings = get_settings()
            cutoff = time.time() - (settings.job_retention_days * 86400)
            purged = purge_old(cutoff, db_path=settings.db_path)
            if purged > 0:
                logger.info(
                    f"Scheduled retention purge: removed {purged} job rows older than {settings.job_retention_days} days (cutoff: {cutoff:.0f})"
                )
            else:
                logger.debug(f"Scheduled retention purge: 0 rows removed (cutoff: {cutoff:.0f})")

            # Build daily rollups on schedule
            rollups_cnt = build_daily_rollups(db_path=settings.db_path)
            logger.info(f"Scheduled daily rollups built: updated {rollups_cnt} hourly buckets")

            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            logger.info("Retention sweeper loop cancelled")
            break
        except Exception as exc:
            logger.error(f"Error in retention sweeper loop: {exc}", exc_info=True)
            await asyncio.sleep(60)




async def start_dispatcher() -> None:
    global _worker_tasks, _retention_task, _wake_event
    _wake_event = None
    settings = get_settings()

    reset_count = reset_orphans()
    if reset_count > 0:
        logger.info(f"Reset {reset_count} orphaned running jobs to pending")

    concurrency = settings.max_concurrency
    _worker_tasks = [
        asyncio.create_task(_worker_loop(i)) for i in range(concurrency)
    ]
    _retention_task = asyncio.create_task(_retention_loop())
    logger.info(f"Started dispatcher with {concurrency} workers and retention sweeper ({settings.job_retention_days}d retention)")


async def stop_dispatcher() -> None:
    global _worker_tasks, _retention_task, _wake_event
    _wake_event = None
    if _retention_task:
        _retention_task.cancel()
        try:
            await _retention_task
        except asyncio.CancelledError:
            pass
        _retention_task = None

    for t in _worker_tasks:
        t.cancel()
    if _worker_tasks:
        await asyncio.gather(*_worker_tasks, return_exceptions=True)
    _worker_tasks = []
    logger.info("Stopped dispatcher")


