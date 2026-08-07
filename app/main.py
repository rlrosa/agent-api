import asyncio
import json
import os
import secrets
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

from app.agents import ensure_available, get_agent_availability
from app.attachments import compose_prompt, materialize_attachment
from app.config import get_settings
from app.db import (
    cancel_job,
    create_job,
    get_job,
    init_db,
    list_jobs,
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.settings = settings

    # Ensure DB tables exist
    init_db(settings.db_path)

    # Start background worker dispatcher pool
    await start_dispatcher()
    yield
    # Stop background worker dispatcher pool
    await stop_dispatcher()


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


def verify_api_key(
    request: Request,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    settings = get_settings()
    client_ip = request.client.host if request.client else "unknown"

    # Cloudflare / Tunnel Header Trap: If any Cloudflare header is present, ALWAYS require API key!
    has_cf_header = any(
        k.lower() in ("cf-connecting-ip", "cf-ray", "cf-visitor")
        for k in request.headers.keys()
    )

    trusted = (not has_cf_header) and is_trusted_peer(client_ip, settings.trusted_networks)

    if trusted:
        logger.info(f"Auth BYPASS (trusted peer IP: {client_ip}) for {request.url.path}")
        return "bypass"

    if not x_api_key or not secrets.compare_digest(x_api_key, settings.api_key):
        logger.warning(f"Auth DENIED (peer IP: {client_ip}, CF header: {has_cf_header}) for {request.url.path}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized: Missing or invalid X-API-Key header",
        )

    logger.info(f"Auth OK (API Key provided by {client_ip}) for {request.url.path}")
    return x_api_key



@app.get("/healthz", response_model=HealthResponse)
async def healthz():
    settings = get_settings()
    pending_jobs = list_jobs(status="pending", limit=1000)
    running_jobs = list_jobs(status="running", limit=1000)

    from app.ratelimit import rate_limit_manager

    return HealthResponse(
        version="0.1.0",
        max_concurrency=settings.max_concurrency,
        queue_depth=len(pending_jobs),
        running_count=len(running_jobs),
        effective_concurrency=rate_limit_manager.effective_concurrency,
        agents=get_agent_availability(),
    )



@app.post("/v1/jobs")
async def create_job_endpoint(
    request: Request,
    api_key: str = Depends(verify_api_key),
):
    settings = get_settings()
    content_type = request.headers.get("content-type", "").lower()

    agent_name: Optional[str] = None
    prompt: str = ""
    model: Optional[str] = None
    effort: Optional[str] = None
    attachments_input: List[Dict[str, Any]] = []

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

    # Validate agent availability -> 400 Bad Request if missing/unavailable
    try:
        ensure_available(target_agent)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))

    # Clamp wait_time to WAIT_MAX
    effective_wait = min(max(0, wait_time), settings.wait_max)

    job_id = str(uuid.uuid4())
    ws_path = os.path.join(settings.work_root, job_id)

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

    # Create job row in DB with fully-composed prompt
    job = create_job(
        agent=target_agent,
        prompt=final_prompt,
        model=model,
        effort=effort,
        metadata=metadata,
        wait=effective_wait,
        timeout=job_timeout,
        db_path=settings.db_path,
        job_id=job_id,
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
    api_key: str = Depends(verify_api_key),
):
    settings = get_settings()
    job = get_job(job_id, db_path=settings.db_path)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    return job


@app.get("/v1/jobs")
async def list_jobs_endpoint(
    status_filter: Optional[str] = Query(None, alias="status"),
    limit: int = Query(default=50, ge=1, le=500),
    api_key: str = Depends(verify_api_key),
):
    settings = get_settings()
    return list_jobs(status=status_filter, limit=limit, db_path=settings.db_path)


@app.delete("/v1/jobs/{job_id}")
async def cancel_job_endpoint(
    job_id: str,
    api_key: str = Depends(verify_api_key),
):
    settings = get_settings()
    job = get_job(job_id, db_path=settings.db_path)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

    from app.runner import kill_active_job_process
    kill_active_job_process(job_id)

    canceled = cancel_job(job_id, db_path=settings.db_path)
    return canceled

