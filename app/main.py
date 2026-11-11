import asyncio
import json
import os
import secrets
import time
import uuid

from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional, Union

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)

from app.agents import (
    ensure_available,
    get_agent_availability,
    resolve_effort,
    resolve_model,
    validate_agent_model,
)
from app.attachments import compose_prompt, materialize_attachment
from app.config import get_settings

from app.db import (
    cancel_job,
    create_job,
    get_job,
    init_db,
    list_jobs,
    list_queue_jobs,
    schedule_retry,
)
from app.dispatcher import (
    register_waiter,
    start_dispatcher,
    stop_dispatcher,
    unregister_waiter,
    wake_dispatcher,
)
from app.models import (
    CreateJobRequest,
    HealthResponse,
)


def format_truncated_prompt(prompt: str, max_chars: int) -> str:
    if max_chars <= 0:
        return "[disabled]"
    clean_prompt = " ".join(prompt.split())
    if len(clean_prompt) > max_chars:
        return f"{clean_prompt[:max_chars]}... (truncated)"
    return clean_prompt


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.settings = settings

    log_level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        force=True,
    )
    logging.getLogger("agent-api").setLevel(log_level)
    logging.getLogger("uvicorn").setLevel(log_level)

    from app.events import setup_async_logging, stop_async_logging
    setup_async_logging(log_level=settings.log_level)

    # Ensure DB tables exist
    init_db(settings.db_path)

    # Start background worker dispatcher pool
    await start_dispatcher()
    yield
    # Stop background worker dispatcher pool
    await stop_dispatcher()
    stop_async_logging()



app = FastAPI(title="agent-api", version="0.1.0", lifespan=lifespan)



import ipaddress
import logging

logger = logging.getLogger("agent_api.auth")


def is_trusted_peer(client_ip: str, trusted_networks: List[str]) -> bool:
    try:
        ip = ipaddress.ip_address(client_ip)
        for net_str in trusted_networks:
            try:
                net = ipaddress.ip_network(net_str, strict=False)
                if ip in net:
                    return True
            except ValueError:
                pass
    except ValueError:
        pass
    return False


_security_auth_failures: List[Dict[str, Any]] = []


def record_auth_failure(peer_ip: str, reason: str, path: str):
    _security_auth_failures.append({
        "ts": time.time(),
        "peer_ip": peer_ip,
        "reason": reason,
        "path": path,
    })
    if len(_security_auth_failures) > 1000:
        _security_auth_failures.pop(0)


def get_security_stats(since: Optional[float] = None) -> Dict[str, Any]:
    cutoff = since if since is not None else (time.time() - 86400)
    recent = [f for f in _security_auth_failures if f["ts"] >= cutoff]
    failures_by_ip: Dict[str, int] = {}
    for f in recent:
        ip = f["peer_ip"]
        failures_by_ip[ip] = failures_by_ip.get(ip, 0) + 1

    from app.runner import check_bwrap_confinement
    return {
        "since": cutoff,
        "total_failures": len(recent),
        "failures_by_ip": failures_by_ip,
        "failures": recent[-50:],
        "confinement": check_bwrap_confinement(),
    }


from app.security import ALL_SCOPES, AuthContext, authenticate_key_info, rate_limiter


def verify_api_key(
    request: Request,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
) -> AuthContext:
    settings = get_settings()
    client_ip = request.client.host if request.client else "unknown"

    # Cloudflare / Tunnel Header Trap: If any Cloudflare header is present, ALWAYS require API key!
    has_cf_header = any(
        k.lower() in ("cf-connecting-ip", "cf-ray", "cf-visitor")
        for k in request.headers.keys()
    )

    trusted = (not has_cf_header) and is_trusted_peer(client_ip, settings.trusted_networks)

    if trusted:
        auth_ctx = AuthContext(name="bypass", scopes=ALL_SCOPES)
    else:
        key_info = authenticate_key_info(x_api_key)
        if not key_info:
            reason = "Missing or invalid X-API-Key"
            record_auth_failure(client_ip, reason, request.url.path)
            logger.warning(f"Auth DENIED (peer IP: {client_ip}, CF header: {has_cf_header}) for {request.url.path}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized: Missing or invalid X-API-Key header",
            )
        auth_ctx = AuthContext(name=key_info.name, scopes=key_info.scopes)
        logger.info(f"Auth OK (Key name '{key_info.name}' provided by {client_ip}) for {request.url.path}")

    # Sliding-window rate limit (skip for /healthz and /dashboard)
    if request.url.path not in ("/healthz", "/dashboard"):
        caller_id = auth_ctx.name if auth_ctx.name != "bypass" else f"ip_{client_ip}"
        is_limited, retry_after = rate_limiter.is_rate_limited(caller_id, settings.rate_limit_per_min)
        if is_limited:
            logger.warning(f"Rate limit exceeded for caller '{caller_id}' (IP: {client_ip}) on {request.url.path}")
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded",
                headers={"Retry-After": str(retry_after)},
            )

    return auth_ctx


def require_scope(scope: str):
    def dependency(auth: AuthContext = Depends(verify_api_key)) -> AuthContext:
        if scope not in auth.scopes:
            logger.warning(f"Forbidden: Caller '{auth.name}' missing required scope '{scope}' on request")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Forbidden: Missing required scope '{scope}'",
            )
        return auth
    return dependency


def check_job_modify_auth(auth: AuthContext) -> str:
    """
    Returns the api_key_name filter for db operations.
    If caller has admin scope, returns 'bypass' (allowing cross-tenant modify).
    If caller has jobs scope, returns auth.name (enforcing tenant isolation).
    If caller has neither, raises 403 Forbidden.
    """
    if "admin" in auth.scopes:
        return "bypass"
    if "jobs" in auth.scopes:
        return auth.name
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Forbidden: Missing required scope ('jobs' or 'admin')",
    )




@app.get("/healthz", response_model=HealthResponse)
async def healthz():
    settings = get_settings()
    pending_jobs = list_jobs(status="pending", limit=1000)
    running_jobs = list_jobs(status="running", limit=1000)

    from app.ratelimit import rate_limit_manager
    from app.runner import check_bwrap_confinement

    return HealthResponse(
        version="0.1.0",
        max_concurrency=settings.max_concurrency,
        queue_depth=len(pending_jobs),
        running_count=len(running_jobs),
        effective_concurrency=rate_limit_manager.effective_concurrency,
        agents=get_agent_availability(),
        confinement=check_bwrap_confinement(),
    )




@app.post("/v1/jobs")
async def create_job_endpoint(
    request: Request,
    auth: AuthContext = Depends(require_scope("jobs")),
):
    settings = get_settings()
    content_type = request.headers.get("content-type", "")

    attachments_input: List[Dict[str, Any]] = []
    agent_name: Optional[str] = None
    prompt: str = ""
    model: Optional[str] = None
    effort: Optional[str] = None
    wait_time: int = settings.wait_default
    job_timeout: int = settings.job_timeout
    metadata: Optional[Dict[str, Any]] = None

    if "multipart/form-data" in content_type:
        form = await request.form()
        agent_name = form.get("agent")
        prompt = form.get("prompt", "")
        model = form.get("model")
        effort = form.get("effort")
        wait_val = form.get("wait")

        if wait_val is not None:
            try:
                wait_time = int(wait_val)
            except ValueError:
                pass
        timeout_val = form.get("timeout")
        if timeout_val is not None:
            try:
                job_timeout = int(timeout_val)
            except ValueError:
                pass
        meta_val = form.get("metadata")
        if meta_val:
            try:
                metadata = json.loads(meta_val)
            except json.JSONDecodeError:
                pass

        # Handle uploaded files in form data
        for field_name, field_value in form.multi_items():
            if hasattr(field_value, "filename") and getattr(field_value, "filename", None):
                file_bytes = await field_value.read()
                attachments_input.append({
                    "filename": field_value.filename or "uploaded_file.bin",
                    "bytes": file_bytes,
                })


    else:
        try:
            body_json = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON body")

        try:
            job_req = CreateJobRequest(**body_json)
        except Exception as err:
            raise HTTPException(status_code=422, detail=f"Invalid request parameters: {err}")

        agent_name = job_req.agent
        prompt = job_req.prompt
        model = job_req.model
        effort = job_req.effort
        wait_time = job_req.wait
        job_timeout = job_req.timeout
        metadata = job_req.metadata
        if job_req.attachments:
            for att in job_req.attachments:
                att_dict = att.model_dump(exclude_none=True)
                if att_dict:
                    attachments_input.append(att_dict)

    if not prompt:
        raise HTTPException(status_code=400, detail="Field 'prompt' is required")

    target_agent = agent_name or settings.default_agent

    # Validate agent availability -> 400 Bad Request if missing
    try:
        ensure_available(target_agent)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))

    resolved_model = resolve_model(target_agent, model) if model else None
    resolved_effort = resolve_effort(effort) if effort else None

    if model and model != resolved_model:
        logger.info(
            f"Fuzzy model resolution for agent '{target_agent}': requested '{model}' -> resolved '{resolved_model}'"
        )
    if effort and effort != resolved_effort:
        logger.info(
            f"Fuzzy effort resolution for agent '{target_agent}': requested '{effort}' -> resolved '{resolved_effort}'"
        )

    # Clamp wait_time to WAIT_MAX
    effective_wait = min(max(0, wait_time), settings.wait_max)

    job_id = str(uuid.uuid4())
    ws_path = os.path.join(settings.work_root, job_id)

    client_ip = request.client.host if request.client else "unknown"
    auth_mode_str = "bypass" if auth == "bypass" else "key"
    att_count = len(attachments_input)
    trunc_prompt = format_truncated_prompt(prompt, settings.log_prompt_chars)
    logger.info(
        f"Job {job_id} submitted by {client_ip} (agent={target_agent}, model={resolved_model or 'default'}, "
        f"effort={resolved_effort or 'default'}, attachments={att_count}, prompt=\"{trunc_prompt}\")"
    )
    logger.debug(f"Job {job_id} submission details: wait={effective_wait}, timeout={job_timeout}, metadata={metadata}")


    # Handle attachments if any
    saved_filenames = []
    total_bytes = 0
    if attachments_input:
        allowlist_hosts = [h.strip() for h in os.environ.get("ATTACHMENT_HOST_ALLOWLIST", "").split(",") if h.strip()]
        try:
            for spec in attachments_input:
                fn, size = await materialize_attachment(
                    spec=spec,
                    workspace_path=ws_path,
                    total_bytes_so_far=total_bytes,
                    allowlist=allowlist_hosts if allowlist_hosts else None,
                )
                saved_filenames.append(fn)
                total_bytes += size
        except ValueError as err:
            raise HTTPException(status_code=400, detail=f"Attachment error: {err}")

    final_prompt = compose_prompt(prompt, saved_filenames, workspace_path=ws_path) if saved_filenames else prompt

    # Check MAX_PENDING_JOBS cap
    pending_jobs = list_jobs(status="pending", limit=settings.max_pending_jobs + 1, db_path=settings.db_path)
    if len(pending_jobs) >= settings.max_pending_jobs:
        logger.warning(f"Pending queue limit reached ({settings.max_pending_jobs}). Rejecting submission.")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Queue capacity exceeded: maximum pending jobs limit reached",
            headers={"Retry-After": "10"},
        )

    # Create job row in DB with fully-composed prompt, client_ip, auth_mode, and api_key_name
    job = create_job(
        agent=target_agent,
        prompt=final_prompt,
        model=resolved_model,
        effort=resolved_effort,
        metadata=metadata,
        wait=effective_wait,
        timeout=job_timeout,
        db_path=settings.db_path,
        job_id=job_id,
        client_ip=client_ip,
        auth_mode=auth_mode_str,
        api_key_name=auth,
    )




    # Wake dispatcher to claim job
    wake_dispatcher()

    if effective_wait == 0:
        return Response(
            content=json.dumps({"job_id": job["id"], "status": "pending"}),
            status_code=status.HTTP_202_ACCEPTED,
            media_type="application/json",
        )

    # Register waiter and await Future
    fut = await register_waiter(job["id"])
    try:
        res = await asyncio.wait_for(asyncio.shield(fut), timeout=float(effective_wait))
        return res
    except asyncio.TimeoutError:
        await unregister_waiter(job["id"], fut)
        latest_job = get_job(job["id"], db_path=settings.db_path) or job
        return Response(
            content=json.dumps({"job_id": job["id"], "status": latest_job["status"]}),
            status_code=status.HTTP_202_ACCEPTED,
            media_type="application/json",
        )


@app.get("/v1/jobs/{job_id}")
async def get_job_endpoint(
    job_id: str,
    auth: AuthContext = Depends(require_scope("jobs")),
):
    settings = get_settings()
    tenant_filter = "bypass" if "admin" in auth.scopes else auth.name
    job = get_job(job_id, db_path=settings.db_path, api_key_name=tenant_filter)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    return job


@app.get("/v1/jobs")
async def list_jobs_endpoint(
    status_filter: Optional[str] = Query(None, alias="status"),
    limit: int = Query(default=50, ge=1, le=500),
    auth: AuthContext = Depends(require_scope("jobs")),
):
    settings = get_settings()
    return list_jobs(status=status_filter, limit=limit, db_path=settings.db_path, api_key_name=auth.name)


@app.delete("/v1/jobs/{job_id}")
async def cancel_job_endpoint(
    job_id: str,
    auth: AuthContext = Depends(verify_api_key),
):
    settings = get_settings()
    tenant_filter = check_job_modify_auth(auth)
    job = get_job(job_id, db_path=settings.db_path, api_key_name=tenant_filter)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

    from app.runner import kill_active_job_process
    kill_active_job_process(job_id)

    canceled = cancel_job(job_id, db_path=settings.db_path, api_key_name=tenant_filter)
    return canceled


@app.post("/v1/jobs/{job_id}/retry")
async def retry_job_endpoint(
    job_id: str,
    auth: AuthContext = Depends(verify_api_key),
):
    settings = get_settings()
    tenant_filter = check_job_modify_auth(auth)
    job = get_job(job_id, db_path=settings.db_path, api_key_name=tenant_filter)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

    job_status = job.get("status")
    if job_status in ("pending", "running"):
        raise HTTPException(
            status_code=409,
            detail=f"Cannot retry job '{job_id}' in non-terminal state '{job_status}'. Only terminal states ('failed', 'canceled', 'completed') may be retried.",
        )

    requeued = schedule_retry(
        job_id,
        next_attempt_at=time.time(),
        db_path=settings.db_path,
        reset_attempts=True,
    )
    wake_dispatcher()
    return requeued


from app.stats import (
    get_summary_stats,
    get_timeseries_stats,
    get_load_stats,
    get_sources_stats,
    get_model_stats,
    get_error_stats,
)


@app.get("/v1/me")
async def me_endpoint(
    auth: AuthContext = Depends(verify_api_key),
):
    return {
        "key_name": auth.name,
        "scopes": sorted(list(auth.scopes)),
    }


@app.get("/v1/stats/summary")
async def stats_summary_endpoint(
    from_ts: Optional[float] = Query(None, alias="from"),
    to_ts: Optional[float] = Query(None, alias="to"),
    auth: AuthContext = Depends(require_scope("stats")),
):
    settings = get_settings()
    return await asyncio.to_thread(get_summary_stats, from_ts=from_ts, to_ts=to_ts, db_path=settings.db_path)


@app.get("/v1/stats/timeseries")
async def stats_timeseries_endpoint(
    bucket: str = Query("day"),
    from_ts: Optional[float] = Query(None, alias="from"),
    to_ts: Optional[float] = Query(None, alias="to"),
    auth: AuthContext = Depends(require_scope("stats")),
):
    settings = get_settings()
    return await asyncio.to_thread(get_timeseries_stats, bucket=bucket, from_ts=from_ts, to_ts=to_ts, db_path=settings.db_path)


@app.get("/v1/stats/load")
async def stats_load_endpoint(
    by: str = Query("dow_hour"),
    auth: AuthContext = Depends(require_scope("stats")),
):
    settings = get_settings()
    return await asyncio.to_thread(get_load_stats, by=by, db_path=settings.db_path)


@app.get("/v1/stats/sources")
async def stats_sources_endpoint(
    by: Optional[str] = Query(None),
    from_ts: Optional[float] = Query(None, alias="from"),
    to_ts: Optional[float] = Query(None, alias="to"),
    auth: AuthContext = Depends(require_scope("stats")),
):
    settings = get_settings()
    return await asyncio.to_thread(get_sources_stats, by=by, from_ts=from_ts, to_ts=to_ts, db_path=settings.db_path)


@app.get("/v1/stats/models")
async def stats_models_endpoint(
    from_ts: Optional[float] = Query(None, alias="from"),
    to_ts: Optional[float] = Query(None, alias="to"),
    auth: AuthContext = Depends(require_scope("stats")),
):
    settings = get_settings()
    return await asyncio.to_thread(get_model_stats, from_ts=from_ts, to_ts=to_ts, db_path=settings.db_path)


@app.get("/v1/stats/errors")
async def stats_errors_endpoint(
    from_ts: Optional[float] = Query(None, alias="from"),
    to_ts: Optional[float] = Query(None, alias="to"),
    auth: AuthContext = Depends(require_scope("stats")),
):
    settings = get_settings()
    return await asyncio.to_thread(get_error_stats, from_ts=from_ts, to_ts=to_ts, db_path=settings.db_path)


@app.get("/v1/stats/security")
async def stats_security_endpoint(
    since: Optional[float] = Query(None),
    auth: AuthContext = Depends(require_scope("stats")),
):
    return get_security_stats(since=since)


from app.agy_quota import get_agy_quota


@app.get("/v1/stats/agy-quota")
async def stats_agy_quota_endpoint(
    auth: AuthContext = Depends(require_scope("stats")),
):
    return await get_agy_quota()


@app.get("/v1/stats/queue")
async def stats_queue_endpoint(
    status_filter: Optional[str] = Query(None, alias="status"),
    limit: int = Query(default=50, ge=1, le=500),
    auth: AuthContext = Depends(require_scope("stats")),
):
    settings = get_settings()
    return await asyncio.to_thread(
        list_queue_jobs,
        status=status_filter,
        limit=limit,
        db_path=settings.db_path,
    )


from app.events import get_logs



@app.get("/v1/logs")
async def get_logs_endpoint(
    level: Optional[str] = Query(None),
    since: Optional[float] = Query(None),
    job_id: Optional[str] = Query(None),
    limit: int = Query(default=100, ge=1, le=500),
    auth: AuthContext = Depends(require_scope("stats")),
):
    return await asyncio.to_thread(get_logs, level=level, since=since, job_id=job_id, limit=limit)


from fastapi.responses import HTMLResponse


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_endpoint(
    request: Request,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    settings = get_settings()
    client_ip = request.client.host if request.client else "unknown"

    has_cf_header = any(
        k.lower() in ("cf-connecting-ip", "cf-ray", "cf-visitor")
        for k in request.headers.keys()
    )

    trusted = (not has_cf_header) and is_trusted_peer(client_ip, settings.trusted_networks)

    if not trusted:
        from app.security import authenticate_key_info
        key_info = authenticate_key_info(x_api_key)
        if not key_info or "stats" not in key_info.scopes:
            record_auth_failure(client_ip, "Unauthorized dashboard access", request.url.path)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized: Dashboard requires valid key with 'stats' scope",
            )

    dash_path = os.path.join(os.path.dirname(__file__), "static", "dashboard.html")
    if os.path.exists(dash_path):
        with open(dash_path, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    raise HTTPException(status_code=404, detail="Dashboard page not found")




