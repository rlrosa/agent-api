import asyncio
import collections
import json
import logging
import os
import shutil
import signal
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple, Union

from fastapi import HTTPException, status

from app.agents import AGENTS, get_agent_availability, strip_model_tier
from app.attachments import compose_prompt, materialize_attachment
from app.config import Settings, get_settings
from app.db import (
    close_session_row,
    create_session_row,
    create_session_turn,
    finish_session_turn,
    get_session,
    list_session_turns,
    reset_orphan_sessions,
    update_session_state,
)
from app.models import AttachmentSpec
from app.ratelimit import is_rate_limit_error, rate_limit_manager
from app.runner import (
    UNREADABLE_ATTACHMENT_PATTERNS,
    build_scrubbed_env,
    find_declared_attachments,
    wrap_cmd_with_bwrap,
)

logger = logging.getLogger("agent-api.sessions")


def compute_cost(
    model: Optional[str],
    input_tokens: int,
    output_tokens: int,
    total_tokens: int,
    model_prices: Dict[str, Any],
) -> Optional[float]:
    if not model or not model_prices:
        return None
    m_key = model.lower()
    base_key = strip_model_tier(m_key)
    price_val = model_prices.get(m_key)
    if price_val is None:
        price_val = model_prices.get(base_key)

    if price_val is not None:
        if isinstance(price_val, dict):
            in_rate = float(price_val.get("input", 0.0))
            out_rate = float(price_val.get("output", 0.0))
            return round(((input_tokens or 0) * in_rate + (output_tokens or 0) * out_rate) / 1_000_000.0, 6)
        elif isinstance(price_val, (int, float)):
            return round((total_tokens or 0) * float(price_val) / 1_000_000.0, 6)
    return None


class AgyStreamDriver:
    """NDJSON protocol driver for agy stream-json mode."""

    def format_prompt(self, prompt: str) -> bytes:
        payload = {"event": "user", "message": {"content": prompt}}
        return (json.dumps(payload) + "\n").encode("utf-8")

    async def parse_response(
        self,
        stdout_reader: asyncio.StreamReader,
        model: str,
        settings: Settings,
        prev_cum_usage: Dict[str, int],
    ) -> Dict[str, Any]:
        conv_id = None
        while True:
            line_bytes = await stdout_reader.readline()
            if not line_bytes:
                raise EOFError("Unexpected EOF from agy stream-json stdout before result event")
            line = line_bytes.decode("utf-8", errors="replace").strip()
            if not line:
                continue

            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue

            event_type = evt.get("event")
            if event_type == "init":
                conv_id = evt.get("conversation_id")
            elif event_type == "result":
                conv_id = evt.get("conversation_id") or conv_id
                response = evt.get("response", "")
                status_val = evt.get("status")
                is_success = (status_val == "SUCCESS")
                error_msg = evt.get("error") if not is_success else None

                usage = evt.get("usage") or {}
                cum_input = usage.get("input_tokens", 0) or 0
                cum_output = usage.get("output_tokens", 0) or 0
                cum_cached = (
                    usage.get("cache_read_tokens")
                    or usage.get("cache_read_input_tokens")
                    or usage.get("cached_tokens", 0)
                ) or 0
                cum_total = usage.get("total_tokens") or (cum_input + cum_output) or 0

                turn_input = max(0, cum_input - prev_cum_usage.get("input_tokens", 0))
                turn_output = max(0, cum_output - prev_cum_usage.get("output_tokens", 0))
                turn_cached = max(0, cum_cached - prev_cum_usage.get("cached_tokens", 0))
                turn_total = max(0, cum_total - prev_cum_usage.get("total_tokens", 0))

                new_cum_usage = {
                    "input_tokens": cum_input,
                    "output_tokens": cum_output,
                    "cached_tokens": cum_cached,
                    "total_tokens": cum_total,
                }
                turn_usage = {
                    "input_tokens": turn_input,
                    "output_tokens": turn_output,
                    "cached_tokens": turn_cached,
                    "total_tokens": turn_total,
                }

                turn_cost = compute_cost(model, turn_input, turn_output, turn_total, settings.model_prices)
                cum_cost = compute_cost(model, cum_input, cum_output, cum_total, settings.model_prices)

                return {
                    "conversation_id": conv_id,
                    "response": response,
                    "is_success": is_success,
                    "error": error_msg,
                    "turn_usage": turn_usage,
                    "cum_usage": new_cum_usage,
                    "turn_cost": turn_cost,
                    "cum_cost": cum_cost,
                }


class ClaudeStreamDriver:
    """NDJSON protocol driver for claude stream-json mode."""

    def format_prompt(self, prompt: str) -> bytes:
        payload = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": prompt}],
            },
        }
        return (json.dumps(payload) + "\n").encode("utf-8")

    async def parse_response(
        self,
        stdout_reader: asyncio.StreamReader,
        model: str,
        settings: Settings,
        prev_cum_usage: Dict[str, int],
        prev_cum_cost: Optional[float],
    ) -> Dict[str, Any]:
        session_id = None
        while True:
            line_bytes = await stdout_reader.readline()
            if not line_bytes:
                raise EOFError("Unexpected EOF from claude stream-json stdout before result event")
            line = line_bytes.decode("utf-8", errors="replace").strip()
            if not line:
                continue

            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue

            evt_type = evt.get("type")
            if evt_type == "system":
                session_id = evt.get("session_id")
            elif evt_type == "result":
                session_id = evt.get("session_id") or session_id
                response = evt.get("result", "")
                is_error = evt.get("is_error", False) or (evt.get("subtype", "success") != "success")
                error_msg = evt.get("error") if is_error else None

                usage = evt.get("usage") or {}
                in_tok = usage.get("input_tokens", 0) or 0
                out_tok = usage.get("output_tokens", 0) or 0
                cache_tok = (usage.get("cache_read_input_tokens") or usage.get("cache_read_tokens", 0)) or 0
                tot_tok = in_tok + out_tok + cache_tok

                upstream_cost = evt.get("total_cost_usd")
                if upstream_cost is not None:
                    try:
                        cum_cost = float(upstream_cost)
                        turn_cost = round(max(0.0, cum_cost - (prev_cum_cost or 0.0)), 6)
                    except (ValueError, TypeError):
                        cum_cost = None
                        turn_cost = None
                else:
                    turn_cost = compute_cost(model, in_tok, out_tok, tot_tok, settings.model_prices)
                    cum_cost = round((prev_cum_cost or 0.0) + turn_cost, 6) if turn_cost is not None else None

                turn_usage = {
                    "input_tokens": in_tok,
                    "output_tokens": out_tok,
                    "cached_tokens": cache_tok,
                    "total_tokens": tot_tok,
                }
                new_cum_usage = {
                    "input_tokens": prev_cum_usage.get("input_tokens", 0) + in_tok,
                    "output_tokens": prev_cum_usage.get("output_tokens", 0) + out_tok,
                    "cached_tokens": prev_cum_usage.get("cached_tokens", 0) + cache_tok,
                    "total_tokens": prev_cum_usage.get("total_tokens", 0) + tot_tok,
                }

                return {
                    "conversation_id": session_id,
                    "response": response,
                    "is_success": not is_error,
                    "error": error_msg,
                    "turn_usage": turn_usage,
                    "cum_usage": new_cum_usage,
                    "turn_cost": turn_cost,
                    "cum_cost": cum_cost,
                }


class LiveSession:
    def __init__(
        self,
        session_id: str,
        agent: str,
        model: Optional[str],
        effort: Optional[str],
        workspace_path: str,
        proc: asyncio.subprocess.Process,
        driver: Union[AgyStreamDriver, ClaudeStreamDriver],
        idle_timeout: int,
        turn_timeout: int,
        api_key_name: Optional[str],
    ):
        self.id = session_id
        self.agent = agent
        self.model = model
        self.effort = effort
        self.workspace_path = workspace_path
        self.proc = proc
        self.driver = driver
        self.idle_timeout = idle_timeout
        self.turn_timeout = turn_timeout
        self.api_key_name = api_key_name

        self.lock = asyncio.Lock()
        self.queued_waiters = 0
        now = time.time()
        self.last_active_at = now
        self.status = "idle"
        self.turn_count = 0
        self.total_attachment_bytes = 0
        self.cumulative_usage = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_tokens": 0,
            "total_tokens": 0,
        }
        self.cumulative_cost: Optional[float] = None
        self.upstream_conversation_id: Optional[str] = None
        self.closed_reason: Optional[str] = None

        self.stderr_buffer = collections.deque(maxlen=200)
        self.drain_task: Optional[asyncio.Task] = None


async def _drain_stderr(proc: asyncio.subprocess.Process, buffer: collections.deque):
    """Continuously drains stderr to prevent child CLI deadlock when buffer >64 KiB."""
    try:
        while proc.returncode is None:
            chunk = await proc.stderr.read(4096)
            if not chunk:
                break
            text = chunk.decode("utf-8", errors="replace")
            buffer.append(text)
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        logger.debug(f"stderr drain finished: {exc}")


class SessionManager:
    def __init__(self):
        self._sessions: Dict[str, LiveSession] = {}
        self._lock: Optional[asyncio.Lock] = None
        self._reaper_task: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    @property
    def lock(self) -> asyncio.Lock:
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        if self._lock is None or self._loop != current_loop:
            self._lock = asyncio.Lock()
            self._loop = current_loop
        return self._lock

    def is_session_active(self, session_id: str) -> bool:
        return session_id in self._sessions

    def active_session_count(self) -> int:
        return len(self._sessions)

    async def start(self):
        current_loop = asyncio.get_running_loop()
        settings = get_settings()
        reset_count = reset_orphan_sessions(settings.db_path)
        if reset_count > 0:
            logger.info(f"Startup session sweep: reset {reset_count} orphaned sessions to closed")

        reaped = sweep_orphaned_session_workspaces(db_path=settings.db_path)
        if reaped > 0:
            logger.info(f"Startup session sweep: reaped {reaped} orphaned session workspace directories")

        if self._reaper_task is not None:
            if self._reaper_task.get_loop() != current_loop or self._reaper_task.done():
                self._reaper_task.cancel()
                self._reaper_task = None

        if self._reaper_task is None:
            self._reaper_task = asyncio.create_task(self._idle_reaper_loop())
            logger.info("Started session idle reaper loop")

    async def stop(self):
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None

        if self._reaper_task:
            task = self._reaper_task
            self._reaper_task = None
            task.cancel()
            if current_loop and task.get_loop() == current_loop:
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        session_ids = list(self._sessions.keys())
        for sid in session_ids:
            try:
                await self.close_session(sid, reason="closed", error="Closed on server shutdown")
            except Exception as exc:
                logger.warning(f"Error stopping session {sid}: {exc}")
        logger.info("Stopped SessionManager")

    async def _idle_reaper_loop(self):
        logger.debug("Idle reaper loop started")
        while True:
            try:
                await asyncio.sleep(1.0)
                now = time.time()
                to_expire = []
                async with self.lock:
                    for sid, s in self._sessions.items():
                        if (
                            not s.lock.locked()
                            and s.queued_waiters == 0
                            and s.status == "idle"
                            and (now - s.last_active_at) >= s.idle_timeout
                        ):
                            to_expire.append(sid)

                for sid in to_expire:
                    logger.info(f"Session '{sid}' expired after {self._sessions[sid].idle_timeout}s idle; reaping")
                    await self.close_session(sid, reason="expired")

            except asyncio.CancelledError:
                logger.debug("Idle reaper loop cancelled")
                break
            except Exception as exc:
                logger.error(f"Error in idle reaper loop: {exc}", exc_info=True)

    async def create_session(
        self,
        agent: str,
        model: Optional[str] = None,
        effort: Optional[str] = None,
        idle_timeout: Optional[int] = None,
        turn_timeout: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
        client_ip: Optional[str] = None,
        auth_mode: Optional[str] = None,
        api_key_name: Optional[str] = None,
        custom_argv: Optional[List[str]] = None,
        db_path: Optional[str] = None,
    ) -> LiveSession:
        settings = get_settings()

        async with self.lock:
            if len(self._sessions) >= settings.max_active_sessions:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=f"Max active sessions limit reached ({settings.max_active_sessions})",
                    headers={"Retry-After": "10"},
                )

            session_id = str(uuid.uuid4())
            workspace_path = os.path.join(settings.session_work_root, session_id)
            attachments_path = os.path.join(workspace_path, "attachments")
            os.makedirs(attachments_path, exist_ok=True)

            # Resolve timeouts
            eff_idle = idle_timeout if idle_timeout is not None else settings.session_idle_timeout
            eff_idle = max(5, min(eff_idle, settings.session_idle_max))
            eff_turn = turn_timeout if turn_timeout is not None else settings.session_turn_timeout

            # Driver selection
            if agent.startswith("mock_claude") or agent == "claude":
                driver = ClaudeStreamDriver()
            else:
                driver = AgyStreamDriver()

            # Argv builder
            if custom_argv is not None:
                raw_argv = custom_argv
            else:
                if settings.allow_mock_agent and agent in ("mock_agy_session", "mock_claude_session"):
                    pass
                builder = AGENTS.get(agent, {}).get("session_argv_builder")
                if not builder:
                    shutil.rmtree(workspace_path, ignore_errors=True)
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Agent '{agent}' does not support interactive sessions",
                    )
                raw_argv = builder(model=model, effort=effort, workspace_path=workspace_path)

            # Confinement fail-closed wrap
            try:
                argv = wrap_cmd_with_bwrap(raw_argv, workspace_path, agent)
            except RuntimeError as conf_err:
                shutil.rmtree(workspace_path, ignore_errors=True)
                logger.error(f"Session {session_id} confinement error: {conf_err}")
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=str(conf_err),
                )

            env = build_scrubbed_env(settings)

            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    cwd=workspace_path,
                    env=env,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                    limit=16 * 1024 * 1024,
                )
            except Exception as exc:
                shutil.rmtree(workspace_path, ignore_errors=True)
                logger.error(f"Failed to spawn session process for {session_id}: {exc}")
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Failed to spawn session subprocess: {exc}",
                )

            live_session = LiveSession(
                session_id=session_id,
                agent=agent,
                model=model,
                effort=effort,
                workspace_path=workspace_path,
                proc=proc,
                driver=driver,
                idle_timeout=eff_idle,
                turn_timeout=eff_turn,
                api_key_name=api_key_name,
            )
            live_session.drain_task = asyncio.create_task(_drain_stderr(proc, live_session.stderr_buffer))

            # Create DB row
            create_session_row(
                agent=agent,
                model=model,
                effort=effort,
                idle_timeout=eff_idle,
                turn_timeout=eff_turn,
                metadata=metadata,
                session_id=session_id,
                client_ip=client_ip,
                auth_mode=auth_mode,
                api_key_name=api_key_name,
                status="idle",
                db_path=db_path,
            )

            self._sessions[session_id] = live_session
            logger.info(f"Created session '{session_id}' (PID: {proc.pid}, agent: {agent}, model: {model})")
            return live_session

    async def execute_turn(
        self,
        session_id: str,
        prompt: str,
        attachments: Optional[List[AttachmentSpec]] = None,
        turn_timeout: Optional[int] = None,
        api_key_name: Optional[str] = None,
        db_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        settings = get_settings()

        async with self.lock:
            live = self._sessions.get(session_id)
            if not live:
                sess_row = get_session(session_id, db_path=db_path, api_key_name=api_key_name)
                if not sess_row:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail=f"Session '{session_id}' not found",
                    )
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Session '{session_id}' is {sess_row.get('status', 'closed')} and cannot accept new messages",
                )

            if live.queued_waiters >= settings.max_queued_turns_per_session:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=f"Session turn queue full (max {settings.max_queued_turns_per_session})",
                    headers={"Retry-After": "5"},
                )
            live.queued_waiters += 1

        try:
            async with live.lock:
                live.queued_waiters -= 1

                # Verify process is still alive and session is active
                if live.status in ("closed", "expired", "failed") or live.proc.returncode is not None:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=f"Session '{session_id}' is {live.status} and cannot accept new messages",
                    )

                live.status = "busy"
                now = time.time()
                live.last_active_at = now
                update_session_state(session_id, status="busy", last_active_at=now, db_path=db_path)

                # AIMD cooldown wait
                if rate_limit_manager.is_in_cooldown():
                    rem = rate_limit_manager.get_cooldown_remaining()
                    await asyncio.sleep(min(rem, float(turn_timeout or live.turn_timeout)))

                # Materialize attachments
                attachment_filenames: List[str] = []
                if attachments:
                    for att in attachments:
                        att_dict = att.model_dump(exclude_none=True) if hasattr(att, "model_dump") else (
                            att if isinstance(att, dict) else dict(att)
                        )
                        saved_path, byte_count = await materialize_attachment(
                            att_dict,
                            live.workspace_path,
                            total_bytes_so_far=live.total_attachment_bytes,
                        )
                        live.total_attachment_bytes += byte_count
                        if live.total_attachment_bytes > settings.max_total_bytes:
                            raise HTTPException(
                                status_code=status.HTTP_400_BAD_REQUEST,
                                detail=f"Cumulative attachment size exceeds max allowed limit ({settings.max_total_bytes} bytes)",
                            )
                        attachment_filenames.append(os.path.basename(saved_path))

                composed_prompt = compose_prompt(prompt, attachment_filenames)

                # Pre-execution attachment guard
                declared = find_declared_attachments(composed_prompt, live.workspace_path)
                for att_path in declared:
                    if not os.path.exists(att_path):
                        err_msg = f"Attachment file missing prior to turn execution: {att_path}"
                        logger.error(f"Session {session_id} pre-execution guard failed: {err_msg}")
                        turn_row = create_session_turn(
                            session_id=session_id,
                            turn_index=live.turn_count,
                            prompt=prompt,
                            status="failed",
                            db_path=db_path,
                        )
                        finish_session_turn(
                            turn_id=turn_row["id"],
                            status="failed",
                            error=err_msg,
                            db_path=db_path,
                        )
                        live.status = "failed"
                        await self.close_session(session_id, reason="failed", error=err_msg, db_path=db_path)
                        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=err_msg)

                # Create running turn row in DB
                turn_index = live.turn_count
                turn_row = create_session_turn(
                    session_id=session_id,
                    turn_index=turn_index,
                    prompt=prompt,
                    status="running",
                    db_path=db_path,
                )

                # Send prompt to stdin
                input_bytes = live.driver.format_prompt(composed_prompt)
                try:
                    live.proc.stdin.write(input_bytes)
                    await live.proc.stdin.drain()
                except (BrokenPipeError, ConnectionResetError) as pipe_err:
                    err_msg = f"Failed writing to session stdin (process died): {pipe_err}"
                    finish_session_turn(turn_id=turn_row["id"], status="failed", error=err_msg, db_path=db_path)
                    live.status = "failed"
                    await self.close_session(session_id, reason="failed", error=err_msg, db_path=db_path)
                    raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=err_msg)

                # Parse stream response with turn_timeout
                eff_timeout = float(turn_timeout or live.turn_timeout)
                try:
                    if isinstance(live.driver, ClaudeStreamDriver):
                        result_data = await asyncio.wait_for(
                            live.driver.parse_response(
                                live.proc.stdout,
                                model=live.model or "",
                                settings=settings,
                                prev_cum_usage=live.cumulative_usage,
                                prev_cum_cost=live.cumulative_cost,
                            ),
                            timeout=eff_timeout,
                        )
                    else:
                        result_data = await asyncio.wait_for(
                            live.driver.parse_response(
                                live.proc.stdout,
                                model=live.model or "",
                                settings=settings,
                                prev_cum_usage=live.cumulative_usage,
                            ),
                            timeout=eff_timeout,
                        )
                except asyncio.TimeoutError:
                    err_msg = f"Turn execution timed out after {eff_timeout}s"
                    logger.error(f"Session {session_id} turn {turn_index} timed out")
                    finish_session_turn(turn_id=turn_row["id"], status="failed", error=err_msg, db_path=db_path)
                    live.status = "failed"
                    await self.close_session(session_id, reason="failed", error=err_msg, db_path=db_path)
                    raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail=err_msg)
                except EOFError as eof_err:
                    err_msg = f"Session process exited unexpectedly mid-turn: {eof_err}"
                    logger.error(f"Session {session_id} EOF: {err_msg}")
                    finish_session_turn(turn_id=turn_row["id"], status="failed", error=err_msg, db_path=db_path)
                    live.status = "failed"
                    await self.close_session(session_id, reason="failed", error=err_msg, db_path=db_path)
                    raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=err_msg)

                # Extract turn details
                response_text = result_data.get("response", "")
                is_success = result_data.get("is_success", True)
                err_text = result_data.get("error")
                recent_stderr = "".join(live.stderr_buffer)

                # Output validation guard
                matched_pat = None
                if response_text:
                    resp_lower = response_text.lower()
                    for pat in UNREADABLE_ATTACHMENT_PATTERNS:
                        if pat in resp_lower:
                            matched_pat = pat
                            break

                if matched_pat:
                    is_success = False
                    err_text = f"Output validation guard failed: agent reported missing attachment ('{matched_pat}')"
                    logger.error(f"Session {session_id} output validation guard failed: {err_text}")

                # Rate limit check (NEVER regex-scan response payload!)
                if not is_success:
                    if is_rate_limit_error(exit_code=1, stdout="", stderr=recent_stderr, error=err_text):
                        await rate_limit_manager.handle_rate_limit(1)
                else:
                    await rate_limit_manager.handle_success()

                turn_usage = result_data.get("turn_usage", {})
                cum_usage = result_data.get("cum_usage", {})
                turn_cost = result_data.get("turn_cost")
                cum_cost = result_data.get("cum_cost")

                live.cumulative_usage = cum_usage
                live.cumulative_cost = cum_cost
                live.turn_count += 1
                if result_data.get("conversation_id"):
                    live.upstream_conversation_id = result_data["conversation_id"]

                # Finish turn row in DB
                turn_status = "completed" if is_success else "failed"
                finished_turn = finish_session_turn(
                    turn_id=turn_row["id"],
                    status=turn_status,
                    response=response_text if is_success else None,
                    stderr=recent_stderr,
                    error=err_text,
                    input_tokens=turn_usage.get("input_tokens"),
                    output_tokens=turn_usage.get("output_tokens"),
                    cached_tokens=turn_usage.get("cached_tokens"),
                    total_tokens=turn_usage.get("total_tokens"),
                    cost_usd=turn_cost,
                    db_path=db_path,
                )

                # Update session row in DB
                now_finish = time.time()
                live.last_active_at = now_finish
                live.status = "idle"
                updated_session = update_session_state(
                    session_id=session_id,
                    status="idle",
                    upstream_conversation_id=live.upstream_conversation_id,
                    turn_count=live.turn_count,
                    total_attachment_bytes=live.total_attachment_bytes,
                    input_tokens=cum_usage.get("input_tokens"),
                    output_tokens=cum_usage.get("output_tokens"),
                    cached_tokens=cum_usage.get("cached_tokens"),
                    total_tokens=cum_usage.get("total_tokens"),
                    cost_usd=cum_cost,
                    error=err_text if not is_success else None,
                    last_active_at=now_finish,
                    db_path=db_path,
                )

                if not is_success:
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail=err_text or "Session turn failed",
                    )

                return {
                    **updated_session,
                    "latest_turn": finished_turn,
                }
        finally:
            pass

    async def close_session(
        self,
        session_id: str,
        reason: str = "closed",
        error: Optional[str] = None,
        api_key_name: Optional[str] = None,
        db_path: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        settings = get_settings()

        async with self.lock:
            live = self._sessions.pop(session_id, None)

        if not live:
            # Check DB to see if already closed or exists
            sess_row = get_session(session_id, db_path=db_path, api_key_name=api_key_name)
            if not sess_row:
                return None
            if sess_row.get("status") in ("closed", "expired", "failed"):
                return sess_row
            return close_session_row(
                session_id=session_id,
                status=reason,
                error=error,
                db_path=db_path,
                api_key_name=api_key_name,
            )

        live.status = reason
        live.closed_reason = reason

        # Terminate drain task
        if live.drain_task and not live.drain_task.done():
            live.drain_task.cancel()

        # Attempt graceful stdin EOF exit if idle
        clean_exit = False
        if reason in ("closed", "expired") and not live.lock.locked():
            try:
                if live.proc.stdin and not live.proc.stdin.is_closing():
                    live.proc.stdin.close()
                    await live.proc.stdin.wait_closed()
                await asyncio.wait_for(live.proc.wait(), timeout=2.0)
                clean_exit = True
            except Exception:
                clean_exit = False

        # Escalation kill if not cleanly exited
        if not clean_exit and live.proc.returncode is None:
            try:
                pgid = os.getpgid(live.proc.pid)
                os.killpg(pgid, signal.SIGTERM)
                await asyncio.sleep(0.2)
                if live.proc.returncode is None:
                    os.killpg(pgid, signal.SIGKILL)
                await live.proc.wait()
            except ProcessLookupError:
                pass
            except Exception as exc:
                logger.warning(f"Error escalating kill for session {session_id}: {exc}")

        # Workspace reap
        if not (reason == "failed" and settings.keep_workspace_on_failure):
            try:
                shutil.rmtree(live.workspace_path, ignore_errors=True)
            except Exception as exc:
                logger.warning(f"Failed deleting session workspace {live.workspace_path}: {exc}")

        # Update DB row
        return close_session_row(
            session_id=session_id,
            status=reason,
            error=error,
            db_path=db_path,
            api_key_name=api_key_name,
        )


def sweep_orphaned_session_workspaces(
    work_root: Optional[str] = None,
    db_path: Optional[str] = None,
    min_age_seconds: float = 300.0,
) -> int:
    settings = get_settings()
    root = work_root or settings.session_work_root
    if not os.path.exists(root):
        return 0

    now = time.time()
    reaped = 0

    for item in os.listdir(root):
        sess_dir = os.path.join(root, item)
        if not os.path.isdir(sess_dir):
            continue

        try:
            mtime = os.path.getmtime(sess_dir)
        except OSError:
            continue

        if (now - mtime) < min_age_seconds:
            continue

        if session_manager.is_session_active(item):
            continue

        sess = get_session(item, db_path=db_path)
        if sess is None or sess.get("status") in ("closed", "expired", "failed"):
            try:
                shutil.rmtree(sess_dir, ignore_errors=True)
                reaped += 1
            except Exception as err:
                logger.warning(f"Failed to sweep orphaned session workspace {sess_dir}: {err}")

    return reaped


session_manager = SessionManager()
