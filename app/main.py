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
    AGENTS,
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
    AttachmentSpec,
    CreateJobRequest,
    CreateSessionRequest,
    HealthResponse,
    SessionMessageRequest,
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

    # Start session manager
    from app.sessions import session_manager
    await session_manager.start()
    try:
        # Start background worker dispatcher pool
        await start_dispatcher()
        try:
            yield
        finally:
            await stop_dispatcher()
    finally:
        try:
            await session_manager.stop()
        finally:
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


def _auth_context_for(
    request: Request,
    x_api_key: Optional[str] = None,
) -> AuthContext:
    settings = get_settings()
    client_ip = request.client.host if request.client else "unknown"

    # Cloudflare / Tunnel Header Trap: If any Cloudflare header is present, ALWAYS require API key!
    has_cf_header = any(
        k.lower() in ("cf-connecting-ip", "cf-ray", "cf-visitor")
        for k in request.headers.keys()
    )

    trusted = (not has_cf_header) and is_trusted_peer(client_ip, settings.trusted_networks)

    if x_api_key:
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
        return auth_ctx
    elif trusted:
        return AuthContext(name="bypass", scopes=ALL_SCOPES)
    else:
        reason = "Missing or invalid X-API-Key"
        record_auth_failure(client_ip, reason, request.url.path)
        logger.warning(f"Auth DENIED (peer IP: {client_ip}, CF header: {has_cf_header}) for {request.url.path}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized: Missing or invalid X-API-Key header",
        )


def verify_api_key(
    request: Request,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
) -> AuthContext:
    auth_ctx = _auth_context_for(request, x_api_key)

    # Sliding-window rate limit (skip for /healthz and /dashboard)
    if request.url.path not in ("/healthz", "/dashboard"):
        client_ip = request.client.host if request.client else "unknown"
        caller_id = auth_ctx.name if auth_ctx.name != "bypass" else f"ip_{client_ip}"
        settings = get_settings()
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
    from app.sessions import session_manager

    return HealthResponse(
        version="0.1.0",
        max_concurrency=settings.max_concurrency,
        queue_depth=len(pending_jobs),
        running_count=len(running_jobs),
        effective_concurrency=rate_limit_manager.effective_concurrency,
        agents=get_agent_availability(),
        confinement=check_bwrap_confinement(),
        active_sessions=session_manager.active_session_count(),
        max_active_sessions=settings.max_active_sessions,
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
        form = await _parse_bounded_multipart(
            request,
            max_file_bytes=settings.max_attachment_bytes,
            max_total_bytes=settings.max_total_bytes,
        )
        try:
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
        finally:
            await _close_form_files(form)


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

    if target_agent != "mock_429" and AGENTS.get(target_agent, {}).get("argv_builder") is None:
        raise HTTPException(
            status_code=400,
            detail=f"Agent '{target_agent}' does not support one-shot jobs",
        )

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
        api_key_name=auth.name,
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


# ==============================================================================
# SESSIONS API (/v1/sessions)
# ==============================================================================

from app.models import CreateSessionRequest, SessionMessageRequest
from app.sessions import session_manager
from app.db import get_session, list_sessions, list_session_turns, update_session_state


def _enrich_session_expiry(sess: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not sess:
        return sess
    if sess.get("status") in ("starting", "idle", "busy"):
        sess["expires_at"] = sess["last_active_at"] + sess["idle_timeout"]
        if sess.get("max_lifetime") is not None and sess.get("created_at") is not None:
            sess["max_expires_at"] = sess["created_at"] + sess["max_lifetime"]
    else:
        sess["expires_at"] = None
        sess["max_expires_at"] = None
    return sess


from starlette.formparsers import MultiPartException, MultiPartParser


class _BoundedMultiPartParser(MultiPartParser):
    """Enforces per-file and cumulative attachment byte caps while streaming multipart chunks, before spooling."""

    def __init__(
        self,
        headers: Any,
        stream: Any,
        *,
        max_file_bytes: int,
        max_total_bytes: int,
        initial_total: int = 0,
        max_part_size: int = 1024 * 1024,
    ) -> None:
        super().__init__(headers, stream, max_part_size=max_part_size)
        self.max_file_bytes = max_file_bytes
        self.max_total_bytes = max_total_bytes
        self._running_total = initial_total
        self._current_file_bytes = 0

    def on_part_begin(self) -> None:
        super().on_part_begin()
        self._current_file_bytes = 0

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        if self._current_part.file is not None:
            chunk_len = end - start
            if self._current_file_bytes + chunk_len > self.max_file_bytes:
                raise MultiPartException(
                    f"Attachment error: Attachment exceeds max single file size limit ({self.max_file_bytes} bytes)"
                )
            if self._running_total + self._current_file_bytes + chunk_len > self.max_total_bytes:
                raise MultiPartException(
                    f"Attachment error: Cumulative attachment size exceeds MAX_TOTAL_BYTES ({self.max_total_bytes})"
                )
            self._current_file_bytes += chunk_len
        super().on_part_data(data, start, end)

    def on_part_end(self) -> None:
        if self._current_part.file is not None:
            self._running_total += self._current_file_bytes
        super().on_part_end()


async def _parse_bounded_multipart(
    request: Request,
    *,
    max_file_bytes: int,
    max_total_bytes: int,
    initial_total: int = 0,
) -> Any:
    parser = _BoundedMultiPartParser(
        request.headers,
        request.stream(),
        max_file_bytes=max_file_bytes,
        max_total_bytes=max_total_bytes,
        initial_total=initial_total,
    )
    try:
        return await parser.parse()
    except MultiPartException as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


def _extract_scalar_form_str(form: Any, key: str, default: Optional[str] = None) -> Optional[str]:
    found = False
    last_val: Optional[str] = None
    for k, v in form.multi_items():
        if k != key:
            continue
        if hasattr(v, "filename") or hasattr(v, "file"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Multipart field '{key}' must be scalar text, not a file upload",
            )
        found = True
        last_val = str(v)
    return last_val if found else default


async def _read_bounded_form_uploads(
    form: Any,
    max_file_bytes: int,
    max_total_bytes: int,
    initial_total: int = 0,
) -> List[Dict[str, Any]]:
    uploads: List[Dict[str, Any]] = []
    running_total = initial_total
    try:
        for _, field_value in form.multi_items():
            if hasattr(field_value, "read") and getattr(field_value, "filename", None):
                buf = bytearray()
                while True:
                    chunk = await field_value.read(65536)
                    if not chunk:
                        break
                    if len(buf) + len(chunk) > max_file_bytes:
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"Attachment error: Attachment exceeds max single file size limit ({max_file_bytes} bytes)",
                        )
                    if running_total + len(buf) + len(chunk) > max_total_bytes:
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"Attachment error: Cumulative attachment size exceeds MAX_TOTAL_BYTES ({max_total_bytes})",
                        )
                    buf.extend(chunk)
                running_total += len(buf)
                uploads.append(
                    {
                        "filename": field_value.filename or "uploaded_file.bin",
                        "bytes": bytes(buf),
                    }
                )
        return uploads
    finally:
        for _, field_value in form.multi_items():
            if hasattr(field_value, "close"):
                try:
                    await field_value.close()
                except Exception:
                    pass


async def _close_form_files(form: Any) -> None:
    for _, field_value in form.multi_items():
        if hasattr(field_value, "close"):
            try:
                await field_value.close()
            except Exception:
                pass


def _session_tenant(auth: AuthContext) -> Optional[str]:
    """Return None (unscoped) strictly when caller has 'admin' scope; never map a non-admin key named 'bypass' to unscoped."""
    if "admin" in auth.scopes:
        return None
    if auth.name == "bypass":
        return "__nonadmin_key_bypass__"
    return auth.name


def _suppress_bg_task_exc(task: asyncio.Task) -> None:
    if not task.cancelled():
        task.exception()


@app.post("/v1/sessions")
async def create_session_endpoint(
    request: Request,
    auth: AuthContext = Depends(require_scope("jobs")),
):
    settings = get_settings()
    content_type = request.headers.get("content-type", "")

    agent_name: Optional[str] = None
    prompt: Optional[str] = None
    model: Optional[str] = None
    effort: Optional[str] = None
    attachments_input: List[Any] = []
    idle_timeout: Optional[int] = None
    turn_timeout: Optional[int] = None
    max_lifetime: Optional[int] = None
    wait_time: int = settings.wait_default
    metadata: Optional[Dict[str, Any]] = None

    if "multipart/form-data" in content_type:
        form = await _parse_bounded_multipart(
            request,
            max_file_bytes=settings.max_attachment_bytes,
            max_total_bytes=settings.max_total_bytes,
        )
        try:
            agent_name = _extract_scalar_form_str(form, "agent")
            prompt = _extract_scalar_form_str(form, "prompt")
            model = _extract_scalar_form_str(form, "model")
            effort = _extract_scalar_form_str(form, "effort")
            idle_raw = _extract_scalar_form_str(form, "idle_timeout")
            if idle_raw is not None:
                try:
                    idle_timeout = int(idle_raw)
                except ValueError:
                    pass
            turn_raw = _extract_scalar_form_str(form, "turn_timeout")
            if turn_raw is not None:
                try:
                    turn_timeout = int(turn_raw)
                except ValueError:
                    pass
            max_life_raw = _extract_scalar_form_str(form, "max_lifetime")
            if max_life_raw is not None:
                try:
                    max_lifetime = int(max_life_raw)
                except ValueError:
                    pass
            wait_raw = _extract_scalar_form_str(form, "wait")
            if wait_raw is not None:
                try:
                    wait_time = int(wait_raw)
                except ValueError:
                    pass
            meta_raw = _extract_scalar_form_str(form, "metadata")
            if meta_raw:
                try:
                    metadata = json.loads(meta_raw)
                except Exception:
                    pass

            attachments_input = await _read_bounded_form_uploads(
                form,
                max_file_bytes=settings.max_attachment_bytes,
                max_total_bytes=settings.max_total_bytes,
            )
        finally:
            await _close_form_files(form)
    else:
        try:
            body = await request.json() if await request.body() else {}
        except Exception:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON body")

        try:
            req_obj = CreateSessionRequest(**body)
        except Exception as err:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid request parameters: {err}",
            )

        agent_name = req_obj.agent
        prompt = req_obj.prompt
        model = req_obj.model
        effort = req_obj.effort
        attachments_input = req_obj.attachments or []
        idle_timeout = req_obj.idle_timeout
        turn_timeout = req_obj.turn_timeout
        max_lifetime = req_obj.max_lifetime
        wait_time = req_obj.wait if req_obj.wait is not None else settings.wait_default
        metadata = req_obj.metadata

    agent = agent_name or settings.default_agent
    try:
        ensure_available(agent)
    except ValueError as err:
        logger.error(f"Agent validation failed: {err}")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(err))

    raw_effort = effort or settings.default_effort
    resolved_model = resolve_model(agent, model) if model else None
    resolved_effort = resolve_effort(raw_effort) if raw_effort else None

    eff_idle = idle_timeout if idle_timeout is not None else settings.session_idle_timeout
    eff_idle = max(5, min(eff_idle, settings.session_idle_max))
    eff_turn = turn_timeout if turn_timeout is not None else settings.session_turn_timeout
    max_cap = max(5, settings.session_max_lifetime)
    eff_max_life = max(5, min(max_lifetime if max_lifetime is not None else max_cap, max_cap))
    eff_wait = max(0, min(wait_time, settings.wait_max))

    api_key_name = auth.name
    client_ip = request.client.host if request.client else "unknown"

    live_session = await session_manager.create_session(
        agent=agent,
        model=resolved_model,
        effort=resolved_effort,
        idle_timeout=eff_idle,
        turn_timeout=eff_turn,
        max_lifetime=eff_max_life,
        metadata=metadata,
        client_ip=client_ip,
        auth_mode="bypass" if auth.name == "bypass" else "api_key",
        api_key_name=api_key_name,
        db_path=settings.db_path,
    )

    host_allowlist_raw = os.environ.get("ATTACHMENT_HOST_ALLOWLIST", "")
    allowlist = [h.strip() for h in host_allowlist_raw.split(",") if h.strip()] if host_allowlist_raw else None

    if prompt is None or not prompt.strip():
        if attachments_input:
            async with live_session.lock:
                if live_session.closing or not session_manager.is_session_active(live_session.id):
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=f"Session '{live_session.id}' is {live_session.closed_reason or 'expired'} and cannot accept attachments",
                    )
                live_session.status = "busy"
                live_session.last_active_at = time.time()
                try:
                    for att in attachments_input:
                        if live_session.closing or not session_manager.is_session_active(live_session.id):
                            raise HTTPException(
                                status_code=status.HTTP_409_CONFLICT,
                                detail=f"Session '{live_session.id}' was closed or expired while fetching attachments",
                            )
                        att_dict = att.model_dump(exclude_none=True) if hasattr(att, "model_dump") else (
                            att if isinstance(att, dict) else dict(att)
                        )
                        saved_name, byte_count = await materialize_attachment(
                            att_dict,
                            live_session.workspace_path,
                            total_bytes_so_far=live_session.total_attachment_bytes,
                            allowlist=allowlist,
                        )
                        live_session.managed_attachments.add(
                            os.path.abspath(os.path.join(live_session.workspace_path, "attachments", saved_name))
                        )
                        live_session.total_attachment_bytes += byte_count
                        if live_session.total_attachment_bytes > settings.max_total_bytes:
                            raise ValueError(
                                f"Cumulative attachment size ({live_session.total_attachment_bytes} bytes) exceeds MAX_TOTAL_BYTES ({settings.max_total_bytes})"
                            )
                    if live_session.closing or not session_manager.is_session_active(live_session.id):
                        raise HTTPException(
                            status_code=status.HTTP_409_CONFLICT,
                            detail=f"Session '{live_session.id}' was closed or expired while fetching attachments",
                        )
                    now_done = time.time()
                    live_session.status = "idle"
                    live_session.last_active_at = now_done
                    update_session_state(
                        session_id=live_session.id,
                        status="idle",
                        total_attachment_bytes=live_session.total_attachment_bytes,
                        last_active_at=now_done,
                        db_path=settings.db_path,
                    )
                except ValueError as att_err:
                    await session_manager.close_session(
                        live_session.id,
                        reason="failed",
                        error=str(att_err),
                        db_path=settings.db_path,
                    )
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Attachment error: {att_err}",
                    )
                except HTTPException:
                    raise
                except Exception as init_err:
                    if live_session.closing or not session_manager.is_session_active(live_session.id):
                        raise HTTPException(
                            status_code=status.HTTP_409_CONFLICT,
                            detail=f"Session '{live_session.id}' was closed or expired while initializing attachments",
                        )
                    await session_manager.close_session(
                        live_session.id,
                        reason="failed",
                        error=str(init_err),
                        db_path=settings.db_path,
                    )
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail=f"Attachment initialization failed: {init_err}",
                    )

        if not session_manager.is_session_active(live_session.id):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Session '{live_session.id}' expired before initialization completed",
            )
        sess_dict = _enrich_session_expiry(get_session(live_session.id, db_path=settings.db_path))
        return Response(
            content=json.dumps(sess_dict),
            status_code=status.HTTP_201_CREATED,
            media_type="application/json",
        )

    try:
        admitted_live, turn_row = await session_manager.admit_turn(
            session_id=live_session.id,
            prompt=prompt,
            api_key_name=api_key_name,
            db_path=settings.db_path,
        )
    except Exception:
        await session_manager.close_session(live_session.id, reason="failed", db_path=settings.db_path)
        raise

    turn_task = asyncio.create_task(
        session_manager.execute_turn(
            session_id=live_session.id,
            prompt=prompt,
            attachments=attachments_input,
            turn_timeout=eff_turn,
            api_key_name=api_key_name,
            allowlist=allowlist,
            db_path=settings.db_path,
            _admitted=(admitted_live, turn_row),
        )
    )

    if eff_wait == 0:
        turn_task.add_done_callback(_suppress_bg_task_exc)
        sess_dict = _enrich_session_expiry(get_session(live_session.id, db_path=settings.db_path))
        if sess_dict is not None:
            sess_dict["submitted_turn_id"] = turn_row["id"]
        return Response(
            content=json.dumps(sess_dict),
            status_code=status.HTTP_202_ACCEPTED,
            media_type="application/json",
        )

    try:
        turn_res = await asyncio.wait_for(asyncio.shield(turn_task), timeout=float(eff_wait))
        return _enrich_session_expiry(turn_res)
    except asyncio.TimeoutError:
        turn_task.add_done_callback(_suppress_bg_task_exc)
        fresh_sess = _enrich_session_expiry(get_session(live_session.id, db_path=settings.db_path))
        if fresh_sess is not None:
            fresh_sess["submitted_turn_id"] = turn_row["id"]
        return Response(
            content=json.dumps(fresh_sess),
            status_code=status.HTTP_202_ACCEPTED,
            media_type="application/json",
        )
    except Exception:
        await session_manager.close_session(live_session.id, reason="failed", db_path=settings.db_path)
        raise


@app.post("/v1/sessions/{session_id}/messages")
async def session_message_endpoint(
    session_id: str,
    request: Request,
    auth: AuthContext = Depends(require_scope("jobs")),
):
    settings = get_settings()
    tenant = _session_tenant(auth)
    sess_row = get_session(session_id, db_path=settings.db_path, api_key_name=tenant)
    if not sess_row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session '{session_id}' not found",
        )

    if sess_row.get("status") in ("expired", "closed", "failed") or not session_manager.is_session_active(session_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Session '{session_id}' is {sess_row.get('status', 'closed')} and cannot accept new messages",
        )

    content_type = request.headers.get("content-type", "")
    prompt: str = ""
    attachments_input: List[Any] = []
    turn_timeout: Optional[int] = None
    wait_time: int = settings.wait_default

    if "multipart/form-data" in content_type:
        live_curr = session_manager._sessions.get(session_id)
        initial_bytes = live_curr.total_attachment_bytes if live_curr is not None else int(sess_row.get("total_attachment_bytes") or 0)
        form = await _parse_bounded_multipart(
            request,
            max_file_bytes=settings.max_attachment_bytes,
            max_total_bytes=settings.max_total_bytes,
            initial_total=initial_bytes,
        )
        try:
            prompt = _extract_scalar_form_str(form, "prompt", default="") or ""
            turn_raw = _extract_scalar_form_str(form, "turn_timeout")
            if turn_raw is not None:
                try:
                    turn_timeout = int(turn_raw)
                except ValueError:
                    pass
            wait_raw = _extract_scalar_form_str(form, "wait")
            if wait_raw is not None:
                try:
                    wait_time = int(wait_raw)
                except ValueError:
                    pass

            attachments_input = await _read_bounded_form_uploads(
                form,
                max_file_bytes=settings.max_attachment_bytes,
                max_total_bytes=settings.max_total_bytes,
                initial_total=initial_bytes,
            )
        finally:
            await _close_form_files(form)
    else:
        try:
            body = await request.json() if await request.body() else {}
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid JSON body",
            )
        try:
            req_obj = SessionMessageRequest(**body)
        except Exception as err:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid request parameters: {err}",
            )
        prompt = req_obj.prompt
        attachments_input = req_obj.attachments or []
        turn_timeout = req_obj.turn_timeout
        wait_time = req_obj.wait if req_obj.wait is not None else settings.wait_default

    if not prompt or not prompt.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Field 'prompt' is required and cannot be empty",
        )

    host_allowlist_raw = os.environ.get("ATTACHMENT_HOST_ALLOWLIST", "")
    allowlist = [h.strip() for h in host_allowlist_raw.split(",") if h.strip()] if host_allowlist_raw else None

    eff_wait = max(0, min(wait_time, settings.wait_max))
    api_key_name = tenant

    admitted_live, turn_row = await session_manager.admit_turn(
        session_id=session_id,
        prompt=prompt,
        api_key_name=api_key_name,
        db_path=settings.db_path,
    )

    turn_task = asyncio.create_task(
        session_manager.execute_turn(
            session_id=session_id,
            prompt=prompt,
            attachments=attachments_input,
            turn_timeout=turn_timeout,
            api_key_name=api_key_name,
            allowlist=allowlist,
            db_path=settings.db_path,
            _admitted=(admitted_live, turn_row),
        )
    )

    if eff_wait == 0:
        turn_task.add_done_callback(_suppress_bg_task_exc)
        sess_dict = _enrich_session_expiry(get_session(session_id, db_path=settings.db_path, api_key_name=tenant))
        if sess_dict is not None:
            sess_dict["submitted_turn_id"] = turn_row["id"]
        return Response(
            content=json.dumps(sess_dict),
            status_code=status.HTTP_202_ACCEPTED,
            media_type="application/json",
        )

    try:
        turn_res = await asyncio.wait_for(asyncio.shield(turn_task), timeout=float(eff_wait))
        return _enrich_session_expiry(turn_res)
    except asyncio.TimeoutError:
        turn_task.add_done_callback(_suppress_bg_task_exc)
        fresh_sess = _enrich_session_expiry(get_session(session_id, db_path=settings.db_path, api_key_name=tenant))
        if fresh_sess is not None:
            fresh_sess["submitted_turn_id"] = turn_row["id"]
        return Response(
            content=json.dumps(fresh_sess),
            status_code=status.HTTP_202_ACCEPTED,
            media_type="application/json",
        )


@app.get("/v1/sessions/{session_id}")
async def get_session_endpoint(
    session_id: str,
    include_turns: bool = Query(default=False),
    after_turn: Optional[int] = Query(default=None, ge=0, le=(2**63 - 1)),
    turns_limit: int = Query(default=100, ge=1, le=500),
    auth: AuthContext = Depends(require_scope("jobs")),
):
    settings = get_settings()
    tenant = _session_tenant(auth)
    sess = get_session(session_id, db_path=settings.db_path, api_key_name=tenant)
    if not sess:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session '{session_id}' not found",
        )

    _enrich_session_expiry(sess)

    if include_turns:
        fetched = await asyncio.to_thread(
            list_session_turns,
            session_id,
            db_path=settings.db_path,
            api_key_name=tenant,
            after_turn=after_turn,
            limit=turns_limit + 1,
        ) or []
        has_more = len(fetched) > turns_limit
        turns_page = fetched[:turns_limit]
        sess["turns"] = turns_page
        sess["turns_has_more"] = has_more
        sess["next_after_turn"] = turns_page[-1]["turn_index"] if (has_more and turns_page) else None

    return sess


@app.get("/v1/sessions")
async def list_sessions_endpoint(
    status_filter: Optional[str] = Query(None, alias="status"),
    limit: int = Query(default=50, ge=1, le=500),
    auth: AuthContext = Depends(require_scope("jobs")),
):
    settings = get_settings()
    tenant = _session_tenant(auth)
    sessions = list_sessions(
        status=status_filter,
        limit=limit,
        db_path=settings.db_path,
        api_key_name=tenant,
    )
    for s in sessions:
        _enrich_session_expiry(s)
    return sessions


@app.delete("/v1/sessions/{session_id}")
async def delete_session_endpoint(
    session_id: str,
    auth: AuthContext = Depends(verify_api_key),
):
    settings = get_settings()
    if "admin" not in auth.scopes and "jobs" not in auth.scopes:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: Missing required scope ('jobs' or 'admin')",
        )
    tenant_filter = _session_tenant(auth)
    sess = get_session(session_id, db_path=settings.db_path, api_key_name=tenant_filter)
    if not sess:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Session '{session_id}' not found",
        )

    closed = await session_manager.close_session(
        session_id=session_id,
        reason="closed",
        api_key_name=tenant_filter,
        db_path=settings.db_path,
    )
    return closed or sess


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
    auth_ctx = _auth_context_for(request, x_api_key)
    if "stats" not in auth_ctx.scopes:
        client_ip = request.client.host if request.client else "unknown"
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




