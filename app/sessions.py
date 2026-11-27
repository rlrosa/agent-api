import asyncio
import collections
import json
import logging
import os
import shutil
import signal
import time
import uuid
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from fastapi import HTTPException, status
import httpx

from app.agents import AGENTS, resolve_effective_session_model, strip_model_tier
from app.attachments import (
    _publish_staged_file_with_status,
    compose_prompt,
    deferred_attachment_commits,
    get_upload_staging_dir,
    materialize_attachment,
)
from app.config import Settings, get_settings
from app.db import (
    _UNSET,
    close_session_row,
    complete_turn_and_update_session,
    create_session_row,
    create_session_turn,
    finish_session_turn,
    get_session,
    reset_orphan_sessions,
    update_session_state,
)
from app.models import AttachmentSpec
from app.ratelimit import is_rate_limit_error, rate_limit_manager
from app.runner import (
    UNREADABLE_ATTACHMENT_PATTERNS,
    build_scrubbed_env,
    validate_distinct_work_roots,
    wrap_cmd_with_bwrap,
)
from app.sandbox import cleanup_sandbox_job

logger = logging.getLogger("agent-api.sessions")

MAX_AUTH_DIAG_CHARS = 240
STDERR_AUTH_TAIL_CHARS = 256

AUTH_ERROR_PATTERNS = (
    "authentication required",
    "authentication failed",
    "authentication timed out",
    "not logged in",
    "please run /login",
    "oauth token expired",
    "oauth token has expired",
    "invalid_grant",
    "invalid refresh token",
)


def is_auth_error(*texts: Optional[str]) -> bool:
    combined = "\n".join(t for t in texts if t).lower()
    if not combined:
        return False
    return any(pat in combined for pat in AUTH_ERROR_PATTERNS)


def _truncate_diag(text: Optional[str], limit: int = MAX_AUTH_DIAG_CHARS) -> str:
    if not text:
        return ""
    cleaned = " ".join(ln.strip() for ln in str(text).splitlines() if ln.strip()).strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3] + "..."


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

            if not isinstance(evt, dict):
                raise ValueError(f"Expected JSON object event from agy stream-json, got {type(evt).__name__}")

            event_type = evt.get("event")
            if event_type == "init":
                conv_id = evt.get("conversation_id")
            elif event_type == "result":
                raw_res = evt.get("result")
                if raw_res is not None and not isinstance(raw_res, dict):
                    raise ValueError(f"Expected dict in agy result.result, got {type(raw_res).__name__}")
                res_dict = raw_res if isinstance(raw_res, dict) else evt
                conv_id = res_dict.get("conversation_id") or evt.get("conversation_id") or conv_id
                response = res_dict.get("response", "")
                status_val = res_dict.get("status")
                is_success = (status_val == "SUCCESS")
                error_msg = res_dict.get("error") if not is_success else None

                usage = res_dict.get("usage")
                if usage is not None and not isinstance(usage, dict):
                    raise ValueError(f"Expected dict in agy usage, got {type(usage).__name__}")
                if usage is None:
                    new_cum_usage = {
                        "input_tokens": prev_cum_usage.get("input_tokens", 0),
                        "output_tokens": prev_cum_usage.get("output_tokens", 0),
                        "cached_tokens": prev_cum_usage.get("cached_tokens", 0),
                        "total_tokens": prev_cum_usage.get("total_tokens", 0),
                    }
                    turn_usage = {
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "cached_tokens": 0,
                        "total_tokens": 0,
                    }
                    turn_cost = None
                    cum_cost = compute_cost(
                        model,
                        new_cum_usage["input_tokens"],
                        new_cum_usage["output_tokens"],
                        new_cum_usage["total_tokens"],
                        settings.model_prices,
                    )
                else:
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

            if not isinstance(evt, dict):
                raise ValueError(f"Expected JSON object event from claude stream-json, got {type(evt).__name__}")

            evt_type = evt.get("type")
            if evt_type == "system":
                session_id = evt.get("session_id")
            elif evt_type == "result":
                session_id = evt.get("session_id") or session_id
                response = evt.get("result", "")
                is_error = evt.get("is_error", False) or (evt.get("subtype", "success") != "success")
                error_msg = (
                    evt.get("error")
                    or (str(response).strip() if response else None)
                    or "Claude stream-json turn error"
                ) if is_error else None

                usage = evt.get("usage")
                if usage is not None and not isinstance(usage, dict):
                    raise ValueError(f"Expected dict in claude usage, got {type(usage).__name__}")
                usage = usage or {}
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
        max_lifetime: int = 86400,
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
        self.max_lifetime = max_lifetime
        self.api_key_name = api_key_name

        self.lock = asyncio.Lock()
        self.queued_waiters = 0
        self.next_turn_index = 0
        now = time.time()
        self.created_at = now
        self.last_active_at = now
        self.status = "idle"
        self.closing = False
        self.turn_count = 0
        self.total_attachment_bytes = 0
        self.managed_attachments: Set[str] = set()
        self.cumulative_usage = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_tokens": 0,
            "total_tokens": 0,
        }
        self.cumulative_cost: Optional[float] = None
        self.upstream_conversation_id: Optional[str] = None
        self.closed_reason: Optional[str] = None
        self.auth_abort_reason: Optional[str] = None

        self.stderr_buffer = collections.deque(maxlen=200)
        self.drain_task: Optional[asyncio.Task] = None
        self.active_turn_task: Optional[asyncio.Task] = None


def _rollback_turn_uploads(
    live: "LiveSession",
    saved_this_turn: List[Any],
) -> None:
    """Roll back newly staged files from a failed turn without EVER unlinking exposed workspace paths.
    Only unexposed files inside get_upload_staging_dir(live.workspace_path) may be unlinked; any file already
    exposed in workspace_path (or retaining a staging hard link) keeps its byte charge in total_attachment_bytes."""
    abs_staging = os.path.abspath(get_upload_staging_dir(live.workspace_path))
    for entry in saved_this_turn:
        fp = entry[0]
        bc = entry[1]
        retained_staging = entry[4] if len(entry) >= 5 else None
        abs_fp = os.path.abspath(fp)

        try:
            is_unexposed_staged = os.path.commonpath([abs_fp, abs_staging]) == abs_staging
        except ValueError:
            is_unexposed_staged = False

        if not is_unexposed_staged:
            # Never unlink an exposed path inside workspace_path (zero TOCTOU risk if agent replaced it).
            # Try cleaning up any leftover extra staging hard link, while keeping bc charged because abs_fp was exposed.
            if retained_staging is not None:
                try:
                    os.remove(retained_staging)
                except OSError:
                    pass
            continue

        try:
            os.remove(abs_fp)
            removed = True
        except FileNotFoundError:
            removed = True
        except OSError:
            removed = False

        if removed:
            live.total_attachment_bytes = max(0, live.total_attachment_bytes - bc)


async def _drain_stderr(
    proc: asyncio.subprocess.Process,
    buffer: collections.deque,
    live: Optional[LiveSession] = None,
):
    """Continuously drains stderr to prevent child CLI deadlock when buffer >64 KiB,
    and aborts immediately across chunk boundaries if an interactive CLI login prompt appears."""
    rolling_tail = ""
    try:
        while proc.returncode is None:
            chunk = await proc.stderr.read(4096)
            if not chunk:
                break
            text = chunk.decode("utf-8", errors="replace")
            buffer.append(text)
            if live is not None and live.auth_abort_reason is None:
                window = (rolling_tail + text).lower()
                if "authentication required" in window or "waiting for authentication" in window:
                    diag_src = rolling_tail + text
                    first_line = next(
                        (
                            ln.strip()
                            for ln in diag_src.splitlines()
                            if "authentication required" in ln.lower() or "waiting for authentication" in ln.lower()
                        ),
                        next((ln.strip() for ln in diag_src.splitlines() if ln.strip()), "Authentication required"),
                    )
                    live.auth_abort_reason = _truncate_diag(first_line, MAX_AUTH_DIAG_CHARS) or "Authentication required"
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                    break
                rolling_tail = (rolling_tail + text)[-STDERR_AUTH_TAIL_CHARS:]
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        logger.debug(f"stderr drain finished: {exc}")


def _close_proc_pipes(proc: asyncio.subprocess.Process) -> None:
    try:
        if proc.stdin is not None and not proc.stdin.is_closing():
            proc.stdin.close()
    except Exception:
        pass
    proc_transport = getattr(proc, "_transport", None)
    if proc_transport is not None:
        for fd_num in (0, 1, 2):
            try:
                pipe_tr = proc_transport.get_pipe_transport(fd_num)
                if pipe_tr is not None:
                    pipe_tr.close()
            except Exception:
                pass


async def _terminate_and_reap_proc(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)
        await asyncio.sleep(0.2)
        if proc.returncode is None:
            _close_proc_pipes(proc)
            os.killpg(pgid, signal.SIGKILL)
        await asyncio.wait_for(proc.wait(), timeout=2.0)
    except ProcessLookupError:
        _close_proc_pipes(proc)
        try:
            await asyncio.wait_for(proc.wait(), timeout=0.5)
        except Exception:
            pass
    except Exception:
        _close_proc_pipes(proc)
        try:
            proc.kill()
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except Exception:
            pass


def _account_unremoved_upload_files(live: "LiveSession", exc: BaseException) -> bool:
    """Account for exact files created by a failed upload that could not be unlinked.
    Returns True if any leftover file's byte count could not be determined."""
    uncertain = False
    charged_set: Set[str] = getattr(live, "_charged_unremoved_files", None)
    if charged_set is None:
        charged_set = set()
        setattr(live, "_charged_unremoved_files", charged_set)
    for fp, rem_bytes in getattr(exc, "_unremoved_attachment_files", ()):
        if rem_bytes is None:
            uncertain = True
        elif fp not in charged_set and fp not in live.managed_attachments:
            live.total_attachment_bytes += rem_bytes
            charged_set.add(fp)
    return uncertain


class SessionManager:
    def __init__(self):
        self._sessions: Dict[str, LiveSession] = {}
        self._closing: Dict[str, asyncio.Task] = {}
        self._unreaped_procs: Dict[str, asyncio.subprocess.Process] = {}
        self._pending_db_reconcile: Dict[str, Tuple[Optional[str], str, Optional[str], Optional[str]]] = {}
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
            self._closing = {}
            self._unreaped_procs = {}
        return self._lock

    def _prune_exited_unreaped(self) -> int:
        dead_ids = [sid for sid, proc in self._unreaped_procs.items() if proc.returncode is not None]
        for sid in dead_ids:
            self._unreaped_procs.pop(sid, None)
        return sum(1 for sid in self._unreaped_procs if sid not in self._sessions and sid not in self._closing)

    def is_session_active(self, session_id: str) -> bool:
        live = self._sessions.get(session_id)
        return bool(live is not None and not live.closing)

    def active_session_count(self) -> int:
        extra_unreaped = self._prune_exited_unreaped()
        return len(self._sessions) + len(self._closing) + extra_unreaped

    async def start(self):
        current_loop = asyncio.get_running_loop()
        settings = get_settings()
        validate_distinct_work_roots(settings.work_root, settings.session_work_root)
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

        pending_closes = [t for t in self._closing.values() if not t.done()]
        if pending_closes:
            await asyncio.gather(*pending_closes, return_exceptions=True)

        for sid, proc in list(self._unreaped_procs.items()):
            await _terminate_and_reap_proc(proc)
            if proc.returncode is not None:
                self._unreaped_procs.pop(sid, None)

        self.reconcile_pending_db_writes()
        logger.info("Stopped SessionManager")

    def reconcile_pending_db_writes(self) -> None:
        for sid, (turn_id, status_reason, err_msg, target_db) in list(self._pending_db_reconcile.items()):
            try:
                if turn_id is not None:
                    finish_session_turn(turn_id=turn_id, status="failed", error=err_msg, db_path=target_db)
                if status_reason:
                    close_session_row(
                        session_id=sid,
                        status=status_reason,
                        error=err_msg,
                        db_path=target_db,
                        only_if_non_terminal=True,
                    )
                self._pending_db_reconcile.pop(sid, None)
            except Exception as exc:
                logger.warning(f"Deferred DB reconciliation for session '{sid}' still pending: {exc}")

    async def _retire_on_db_failure(
        self,
        live: "LiveSession",
        turn_id: Optional[str],
        persist_err: BaseException,
        db_path: Optional[str],
        prefix: str = "Failed persisting turn state",
    ) -> HTTPException:
        persist_msg = _truncate_diag(f"{prefix}: {persist_err}", MAX_AUTH_DIAG_CHARS)
        logger.error(f"Session {live.id} persistence failure: {persist_msg}", exc_info=True)
        live.status = "failed"
        self._pending_db_reconcile[live.id] = (turn_id, "failed", persist_msg, db_path)
        turn_ok = turn_id is None
        if turn_id is not None:
            try:
                finish_session_turn(turn_id=turn_id, status="failed", error=persist_msg, db_path=db_path)
                turn_ok = True
            except Exception:
                pass
        sess_closed_row = None
        try:
            sess_closed_row = await self.close_session(
                live.id, reason="failed", error=persist_msg, db_path=db_path
            )
        except Exception:
            pass
        if turn_ok and sess_closed_row is not None:
            self._pending_db_reconcile.pop(live.id, None)
        elif turn_ok:
            self._pending_db_reconcile[live.id] = (None, "failed", persist_msg, db_path)
        return HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=persist_msg,
        )

    async def _idle_reaper_loop(self):
        logger.debug("Idle reaper loop started")
        while True:
            try:
                await asyncio.sleep(1.0)
                self.reconcile_pending_db_writes()
                now = time.time()
                to_expire: List[Tuple[str, str, bool]] = []
                async with self.lock:
                    for sid, s in self._sessions.items():
                        if s.closing:
                            continue
                        if (now - s.created_at) >= s.max_lifetime:
                            to_expire.append((sid, f"max_lifetime >= {s.max_lifetime}s", False))
                        elif not s.lock.locked() and s.queued_waiters == 0 and s.status == "idle":
                            if (now - s.last_active_at) >= s.idle_timeout:
                                to_expire.append((sid, f"idle >= {s.idle_timeout}s", True))

                for sid, why, only_if_idle in to_expire:
                    closed = await self.close_session(sid, reason="expired", only_if_idle=only_if_idle)
                    if closed is not None:
                        logger.info(f"Session '{sid}' expired ({why}); reaped")

                # Retry cleanup of any child that survived an earlier close attempt
                async with self.lock:
                    surviving = [
                        (sid, proc)
                        for sid, proc in list(self._unreaped_procs.items())
                        if sid not in self._sessions and sid not in self._closing
                    ]
                for sid, proc in surviving:
                    if proc.returncode is None:
                        await _terminate_and_reap_proc(proc)
                    if proc.returncode is not None:
                        async with self.lock:
                            self._unreaped_procs.pop(sid, None)

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
        max_lifetime: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
        client_ip: Optional[str] = None,
        auth_mode: Optional[str] = None,
        api_key_name: Optional[str] = None,
        custom_argv: Optional[List[str]] = None,
        db_path: Optional[str] = None,
    ) -> LiveSession:
        settings = get_settings()
        validate_distinct_work_roots(settings.work_root, settings.session_work_root)

        async with self.lock:
            if self.active_session_count() >= settings.max_active_sessions:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=f"Max active sessions limit reached ({settings.max_active_sessions})",
                    headers={"Retry-After": "10"},
                )

            session_id = str(uuid.uuid4())
            workspace_path = os.path.join(settings.session_work_root, session_id)
            attachments_path = os.path.join(workspace_path, "attachments")
            os.makedirs(attachments_path, exist_ok=True)

            eff_idle = idle_timeout if idle_timeout is not None else settings.session_idle_timeout
            eff_idle = max(5, min(eff_idle, settings.session_idle_max))
            eff_turn = turn_timeout if turn_timeout is not None else settings.session_turn_timeout
            max_cap = max(5, settings.session_max_lifetime)
            eff_max_life = max_lifetime if max_lifetime is not None else max_cap
            eff_max_life = max(5, min(eff_max_life, max_cap))

            if agent.startswith("mock_claude") or agent == "claude":
                driver = ClaudeStreamDriver()
            else:
                driver = AgyStreamDriver()

            eff_model = model
            if custom_argv is not None:
                raw_argv = custom_argv
            else:
                builder = AGENTS.get(agent, {}).get("session_argv_builder")
                if not builder:
                    shutil.rmtree(workspace_path, ignore_errors=True)
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Agent '{agent}' does not support interactive sessions",
                    )
                eff_model = resolve_effective_session_model(agent, model=model, effort=effort)
                raw_argv = builder(model=model, effort=effort, workspace_path=workspace_path)

            try:
                argv = wrap_cmd_with_bwrap(raw_argv, workspace_path, agent, job_id=session_id)
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

            self._unreaped_procs[session_id] = proc
            drain_task: Optional[asyncio.Task] = None
            try:
                live_session = LiveSession(
                    session_id=session_id,
                    agent=agent,
                    model=eff_model,
                    effort=effort,
                    workspace_path=workspace_path,
                    proc=proc,
                    driver=driver,
                    idle_timeout=eff_idle,
                    turn_timeout=eff_turn,
                    api_key_name=api_key_name,
                    max_lifetime=eff_max_life,
                )
                drain_task = asyncio.create_task(
                    _drain_stderr(proc, live_session.stderr_buffer, live_session)
                )
                live_session.drain_task = drain_task

                create_session_row(
                    agent=agent,
                    model=eff_model,
                    effort=effort,
                    idle_timeout=eff_idle,
                    turn_timeout=eff_turn,
                    max_lifetime=eff_max_life,
                    metadata=metadata,
                    session_id=session_id,
                    client_ip=client_ip,
                    auth_mode=auth_mode,
                    api_key_name=api_key_name,
                    status="idle",
                    db_path=db_path,
                )
            except BaseException:
                try:
                    if drain_task and not drain_task.done():
                        drain_task.cancel()
                        try:
                            await drain_task
                        except asyncio.CancelledError:
                            pass
                    await _terminate_and_reap_proc(proc)
                finally:
                    if proc.returncode is not None:
                        self._unreaped_procs.pop(session_id, None)
                    shutil.rmtree(workspace_path, ignore_errors=True)
                raise

            self._sessions[session_id] = live_session
            self._unreaped_procs.pop(session_id, None)
            logger.info(f"Created session '{session_id}' (PID: {proc.pid}, agent: {agent}, model: {eff_model})")
            return live_session

    async def _retire_dead_child_and_raise(
        self,
        live: LiveSession,
        db_path: Optional[str] = None,
        turn_id: Optional[str] = None,
    ) -> None:
        await asyncio.sleep(0.02)
        recent_stderr = "".join(live.stderr_buffer)
        bounded_stderr = _truncate_diag(recent_stderr, MAX_AUTH_DIAG_CHARS)
        if live.auth_abort_reason or is_auth_error(recent_stderr):
            diag = _truncate_diag(live.auth_abort_reason or recent_stderr) or "authentication required"
            err_msg = _truncate_diag(f"Agent authentication expired or missing ({live.agent}): {diag}", MAX_AUTH_DIAG_CHARS)
            if turn_id:
                finish_session_turn(turn_id=turn_id, status="failed", stderr=bounded_stderr, error=err_msg, db_path=db_path)
            await self.close_session(live.id, reason="failed", error=err_msg, db_path=db_path)
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=err_msg)

        exit_err = _truncate_diag(f"Session subprocess exited (code {live.proc.returncode})", MAX_AUTH_DIAG_CHARS)
        if turn_id:
            finish_session_turn(turn_id=turn_id, status="failed", stderr=bounded_stderr, error=exit_err, db_path=db_path)
        await self.close_session(live.id, reason="failed", error=exit_err, db_path=db_path)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Session '{live.id}' is failed and cannot accept new messages",
        )

    async def admit_turn(
        self,
        session_id: str,
        prompt: str,
        api_key_name: Optional[str] = None,
        db_path: Optional[str] = None,
    ) -> Tuple[LiveSession, Dict[str, Any]]:
        """Validate session liveness, enforce queue admission, and create the turn row before returning 202 or executing."""
        settings = get_settings()
        dead_live: Optional[LiveSession] = None
        expired_live: Optional[LiveSession] = None

        async with self.lock:
            live = self._sessions.get(session_id)
            if not live or live.closing:
                sess_row = get_session(session_id, db_path=db_path, api_key_name=api_key_name)
                if not sess_row:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail=f"Session '{session_id}' not found",
                    )
                st_label = sess_row.get("status", "closed")
                if st_label not in ("closed", "expired", "failed"):
                    st_label = "closed"
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Session '{session_id}' is {st_label} and cannot accept new messages",
                )

            now = time.time()
            if live.status in ("closed", "expired", "failed"):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Session '{session_id}' is {live.status} and cannot accept new messages",
                )

            if (now - live.created_at) >= live.max_lifetime:
                expired_live = live
            elif live.proc.returncode is not None:
                dead_live = live
            else:
                if live.queued_waiters >= settings.max_queued_turns_per_session:
                    raise HTTPException(
                        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                        detail=f"Session turn queue full (max {settings.max_queued_turns_per_session})",
                        headers={"Retry-After": "5"},
                    )
                reserved_idx = live.next_turn_index
                turn_row = create_session_turn(
                    session_id=session_id,
                    turn_index=reserved_idx,
                    prompt=prompt,
                    status="running",
                    db_path=db_path,
                )
                live.queued_waiters += 1
                live.last_active_at = now
                live.next_turn_index = reserved_idx + 1
                return live, turn_row

        if expired_live is not None:
            await self.close_session(session_id, reason="expired", db_path=db_path)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Session '{session_id}' is expired and cannot accept new messages",
            )

        assert dead_live is not None
        await self._retire_dead_child_and_raise(dead_live, db_path=db_path)
        raise AssertionError("unreachable")

    async def execute_turn(
        self,
        session_id: str,
        prompt: str,
        attachments: Optional[List[AttachmentSpec]] = None,
        turn_timeout: Optional[int] = None,
        api_key_name: Optional[str] = None,
        allowlist: Optional[List[str]] = None,
        db_path: Optional[str] = None,
        _admitted: Optional[Tuple[LiveSession, Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        settings = get_settings()

        if _admitted is not None:
            live, turn_row = _admitted
        else:
            live, turn_row = await self.admit_turn(
                session_id=session_id,
                prompt=prompt,
                api_key_name=api_key_name,
                db_path=db_path,
            )

        turn_id = turn_row["id"]
        turn_index = turn_row["turn_index"]
        decremented_waiter = False

        try:
            async with live.lock:
                live.queued_waiters = max(0, live.queued_waiters - 1)
                decremented_waiter = True
                live.active_turn_task = asyncio.current_task()
                saved_this_turn: List[Any] = []

                try:
                    now_start = time.time()
                    if live.closing or live.status in ("closed", "expired", "failed"):
                        finish_session_turn(
                            turn_id=turn_id,
                            status="failed",
                            error=f"Session {live.closed_reason or live.status} before turn started",
                            db_path=db_path,
                        )
                        raise HTTPException(
                            status_code=status.HTTP_409_CONFLICT,
                            detail=f"Session '{session_id}' is {live.closed_reason or live.status} and cannot accept new messages",
                        )

                    if (now_start - live.created_at) >= live.max_lifetime:
                        finish_session_turn(
                            turn_id=turn_id,
                            status="failed",
                            error="Session max_lifetime reached before turn started",
                            db_path=db_path,
                        )
                        await self.close_session(session_id, reason="expired", db_path=db_path)
                        raise HTTPException(
                            status_code=status.HTTP_409_CONFLICT,
                            detail=f"Session '{session_id}' is expired and cannot accept new messages",
                        )

                    if live.proc.returncode is not None:
                        await self._retire_dead_child_and_raise(live, db_path=db_path, turn_id=turn_id)

                    live.status = "busy"
                    live.last_active_at = now_start
                    live.stderr_buffer.clear()
                    try:
                        update_session_state(session_id, status="busy", last_active_at=now_start, db_path=db_path)
                    except Exception as busy_err:
                        raise await self._retire_on_db_failure(
                            live,
                            turn_id,
                            busy_err,
                            db_path=db_path,
                            prefix="Failed persisting turn busy state",
                        )

                    if rate_limit_manager.is_in_cooldown():
                        rem = rate_limit_manager.get_cooldown_remaining()
                        await asyncio.sleep(min(rem, float(turn_timeout or live.turn_timeout)))

                    if live.closing or live.status in ("closed", "expired", "failed"):
                        finish_session_turn(
                            turn_id=turn_id,
                            status="failed",
                            error=f"Session {live.closed_reason or live.status} during cooldown",
                            db_path=db_path,
                        )
                        raise HTTPException(
                            status_code=status.HTTP_409_CONFLICT,
                            detail=f"Session '{session_id}' is {live.closed_reason or live.status} and cannot accept new messages",
                        )

                    attachment_filenames: List[str] = []
                    if attachments:
                        att_dir_abs = os.path.abspath(os.path.join(live.workspace_path, "attachments"))
                        os.makedirs(att_dir_abs, exist_ok=True)
                        try:
                            with deferred_attachment_commits() as deferred_staged:
                                for att in attachments:
                                    if live.closing or live.status in ("closed", "expired", "failed"):
                                        raise FileNotFoundError("Session closed during attachment upload")
                                    att_dict = att.model_dump(exclude_none=True) if hasattr(att, "model_dump") else (
                                        att if isinstance(att, dict) else dict(att)
                                    )
                                    before_def_len = len(deferred_staged)
                                    saved_name, byte_count = await materialize_attachment(
                                        att_dict,
                                        live.workspace_path,
                                        total_bytes_so_far=live.total_attachment_bytes,
                                        allowlist=allowlist,
                                    )
                                    if len(deferred_staged) > before_def_len:
                                        staged_path, clean_fn = deferred_staged[-1]
                                        full_saved = os.path.abspath(staged_path)
                                    else:
                                        clean_fn = os.path.basename(saved_name)
                                        full_saved = os.path.abspath(os.path.join(att_dir_abs, clean_fn))
                                    try:
                                        st_s = os.lstat(full_saved)
                                        s_dev, s_ino = st_s.st_dev, st_s.st_ino
                                    except OSError:
                                        s_dev, s_ino = None, None
                                    saved_this_turn.append((full_saved, byte_count, s_dev, s_ino))
                                    live.total_attachment_bytes += byte_count
                                    if live.closing or live.status in ("closed", "expired", "failed"):
                                        raise FileNotFoundError("Session closed after attachment upload")
                                    if live.total_attachment_bytes > settings.max_total_bytes:
                                        raise ValueError(
                                            f"Cumulative attachment size ({live.total_attachment_bytes} bytes) exceeds MAX_TOTAL_BYTES ({settings.max_total_bytes})"
                                        )
                                    if len(deferred_staged) <= before_def_len:
                                        attachment_filenames.append(clean_fn)

                                # All attachments succeeded: atomically publish deferred staged files into attachments/.
                                # Keep the device & inode recorded from the staging file (never re-lstat exposed pub_full),
                                # and record any retained staging hard link if staging unlink failed.
                                deferred_map = {os.path.abspath(sp): cfn for sp, cfn in deferred_staged}
                                for idx, entry in enumerate(list(saved_this_turn)):
                                    fp, bc, s_dev, s_ino = entry[0], entry[1], entry[2], entry[3]
                                    if fp in deferred_map:
                                        pub_name, retained_staging = _publish_staged_file_with_status(
                                            fp, att_dir_abs, deferred_map[fp]
                                        )
                                        pub_full = os.path.abspath(os.path.join(att_dir_abs, pub_name))
                                        saved_this_turn[idx] = (pub_full, bc, s_dev, s_ino, retained_staging)
                                        attachment_filenames.append(os.path.basename(pub_name))
                        except (ValueError, httpx.RequestError) as att_err:
                            _rollback_turn_uploads(live, saved_this_turn)
                            saved_this_turn.clear()
                            uncertain = _account_unremoved_upload_files(live, att_err)
                            bounded_att_err = _truncate_diag(f"Attachment error: {att_err}", MAX_AUTH_DIAG_CHARS)
                            if live.closing or live.status in ("closed", "expired", "failed") or not self.is_session_active(session_id):
                                try:
                                    complete_turn_and_update_session(
                                        turn_id=turn_id,
                                        turn_status="failed",
                                        session_id=session_id,
                                        turn_error="Session closed while materializing attachments",
                                        total_attachment_bytes=live.total_attachment_bytes,
                                        db_path=db_path,
                                    )
                                except Exception as db_err:
                                    raise await self._retire_on_db_failure(
                                        live, turn_id, db_err, db_path=db_path, prefix="Failed persisting closed-turn attachment state"
                                    )
                                raise HTTPException(
                                    status_code=status.HTTP_409_CONFLICT,
                                    detail=f"Session '{session_id}' is {live.closed_reason or 'closed'} and cannot accept new messages",
                                )
                            if uncertain:
                                live.status = "failed"
                                try:
                                    finish_session_turn(
                                        turn_id=turn_id,
                                        status="failed",
                                        error=bounded_att_err,
                                        db_path=db_path,
                                    )
                                    closed_unc = await self.close_session(
                                        session_id,
                                        reason="failed",
                                        error=bounded_att_err,
                                        db_path=db_path,
                                    )
                                    if closed_unc is None:
                                        raise RuntimeError("Session close row write deferred")
                                except Exception as db_err:
                                    raise await self._retire_on_db_failure(
                                        live, turn_id, db_err, db_path=db_path, prefix="Failed retiring session after uncertain upload cleanup"
                                    )
                                raise HTTPException(
                                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                                    detail=bounded_att_err,
                                )
                            now_idle = time.time()
                            try:
                                complete_turn_and_update_session(
                                    turn_id=turn_id,
                                    turn_status="failed",
                                    session_id=session_id,
                                    turn_error=bounded_att_err,
                                    session_status="idle",
                                    total_attachment_bytes=live.total_attachment_bytes,
                                    last_active_at=now_idle,
                                    db_path=db_path,
                                )
                            except Exception as db_err:
                                raise await self._retire_on_db_failure(
                                    live, turn_id, db_err, db_path=db_path, prefix="Failed persisting attachment validation failure"
                                )
                            live.status = "idle"
                            live.last_active_at = now_idle
                            http_status = (
                                status.HTTP_400_BAD_REQUEST
                                if isinstance(att_err, ValueError)
                                else status.HTTP_502_BAD_GATEWAY
                            )
                            raise HTTPException(
                                status_code=http_status,
                                detail=f"Attachment error: {att_err}",
                            )
                        except OSError as storage_err:
                            _rollback_turn_uploads(live, saved_this_turn)
                            saved_this_turn.clear()
                            uncertain = _account_unremoved_upload_files(live, storage_err)
                            if live.closing or live.status in ("closed", "expired", "failed") or not self.is_session_active(session_id):
                                try:
                                    complete_turn_and_update_session(
                                        turn_id=turn_id,
                                        turn_status="failed",
                                        session_id=session_id,
                                        turn_error="Session closed while materializing attachments",
                                        total_attachment_bytes=live.total_attachment_bytes,
                                        db_path=db_path,
                                    )
                                except Exception as db_err:
                                    raise await self._retire_on_db_failure(
                                        live, turn_id, db_err, db_path=db_path, prefix="Failed persisting closed-turn storage failure"
                                    )
                                raise HTTPException(
                                    status_code=status.HTTP_409_CONFLICT,
                                    detail=f"Session '{session_id}' is {live.closed_reason or 'closed'} and cannot accept new messages",
                                )
                            bounded_io_err = _truncate_diag(f"Attachment storage error: {storage_err}", MAX_AUTH_DIAG_CHARS)
                            if uncertain:
                                live.status = "failed"
                                try:
                                    finish_session_turn(
                                        turn_id=turn_id,
                                        status="failed",
                                        error=bounded_io_err,
                                        db_path=db_path,
                                    )
                                    closed_unc = await self.close_session(
                                        session_id,
                                        reason="failed",
                                        error=bounded_io_err,
                                        db_path=db_path,
                                    )
                                    if closed_unc is None:
                                        raise RuntimeError("Session close row write deferred")
                                except Exception as db_err:
                                    raise await self._retire_on_db_failure(
                                        live, turn_id, db_err, db_path=db_path, prefix="Failed retiring session after uncertain storage cleanup"
                                    )
                                raise HTTPException(
                                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                                    detail=bounded_io_err,
                                )
                            now_idle = time.time()
                            try:
                                complete_turn_and_update_session(
                                    turn_id=turn_id,
                                    turn_status="failed",
                                    session_id=session_id,
                                    turn_error=bounded_io_err,
                                    session_status="idle",
                                    total_attachment_bytes=live.total_attachment_bytes,
                                    last_active_at=now_idle,
                                    db_path=db_path,
                                )
                            except Exception as db_err:
                                raise await self._retire_on_db_failure(
                                    live, turn_id, db_err, db_path=db_path, prefix="Failed persisting attachment storage failure"
                                )
                            live.status = "idle"
                            live.last_active_at = now_idle
                            raise HTTPException(
                                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                                detail=bounded_io_err,
                            )

                        for fp, *_ in saved_this_turn:
                            live.managed_attachments.add(fp)
                        saved_this_turn.clear()

                    if live.closing or live.status in ("closed", "expired", "failed"):
                        finish_session_turn(
                            turn_id=turn_id,
                            status="failed",
                            error=f"Session {live.closed_reason or live.status} before stdin write",
                            db_path=db_path,
                        )
                        raise HTTPException(
                            status_code=status.HTTP_409_CONFLICT,
                            detail=f"Session '{session_id}' is {live.closed_reason or live.status} and cannot accept new messages",
                        )

                    # Verify only session-managed attachment files (never probe paths parsed from free-form prompt text)
                    for att_path in sorted(live.managed_attachments):
                        if not os.path.exists(att_path):
                            err_msg = _truncate_diag(
                                f"Session attachment missing prior to turn execution: {os.path.basename(att_path)}",
                                MAX_AUTH_DIAG_CHARS,
                            )
                            logger.error(f"Session {session_id} pre-execution guard failed: {err_msg}")
                            finish_session_turn(turn_id=turn_id, status="failed", error=err_msg, db_path=db_path)
                            live.status = "failed"
                            await self.close_session(session_id, reason="failed", error=err_msg, db_path=db_path)
                            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=err_msg)

                    if (time.time() - live.created_at) >= live.max_lifetime:
                        finish_session_turn(
                            turn_id=turn_id,
                            status="failed",
                            error="Session max_lifetime reached before prompt send",
                            db_path=db_path,
                        )
                        await self.close_session(session_id, reason="expired", db_path=db_path)
                        raise HTTPException(
                            status_code=status.HTTP_409_CONFLICT,
                            detail=f"Session '{session_id}' is expired and cannot accept new messages",
                        )

                    composed_prompt = compose_prompt(prompt, attachment_filenames, workspace_path=live.workspace_path)
                    input_bytes = live.driver.format_prompt(composed_prompt)

                    eff_timeout = float(turn_timeout or live.turn_timeout)
                    deadline = time.monotonic() + eff_timeout

                    try:
                        live.proc.stdin.write(input_bytes)
                        await asyncio.wait_for(
                            live.proc.stdin.drain(),
                            timeout=max(0.01, deadline - time.monotonic()),
                        )

                        rem_timeout = max(0.01, deadline - time.monotonic())
                        if isinstance(live.driver, ClaudeStreamDriver):
                            result_data = await asyncio.wait_for(
                                live.driver.parse_response(
                                    live.proc.stdout,
                                    model=live.model or "",
                                    settings=settings,
                                    prev_cum_usage=live.cumulative_usage,
                                    prev_cum_cost=live.cumulative_cost,
                                ),
                                timeout=rem_timeout,
                            )
                        else:
                            result_data = await asyncio.wait_for(
                                live.driver.parse_response(
                                    live.proc.stdout,
                                    model=live.model or "",
                                    settings=settings,
                                    prev_cum_usage=live.cumulative_usage,
                                ),
                                timeout=rem_timeout,
                            )
                    except asyncio.TimeoutError:
                        err_msg = f"Turn execution timed out after {eff_timeout}s"
                        logger.error(f"Session {session_id} turn {turn_index} timed out")
                        finish_session_turn(turn_id=turn_id, status="failed", error=err_msg, db_path=db_path)
                        live.status = "failed"
                        await self.close_session(session_id, reason="failed", error=err_msg, db_path=db_path)
                        raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail=err_msg)
                    except (BrokenPipeError, ConnectionResetError) as pipe_err:
                        if live.closing and live.closed_reason in ("closed", "expired"):
                            finish_session_turn(
                                turn_id=turn_id,
                                status="failed",
                                error=f"Session {live.closed_reason} during active turn",
                                db_path=db_path,
                            )
                            raise HTTPException(
                                status_code=status.HTTP_409_CONFLICT,
                                detail=f"Session '{session_id}' is {live.closed_reason} and cannot accept new messages",
                            )
                        err_msg = _truncate_diag(f"Failed writing to session stdin (process died): {pipe_err}", MAX_AUTH_DIAG_CHARS)
                        finish_session_turn(turn_id=turn_id, status="failed", error=err_msg, db_path=db_path)
                        live.status = "failed"
                        await self.close_session(session_id, reason="failed", error=err_msg, db_path=db_path)
                        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=err_msg)
                    except EOFError as eof_err:
                        await asyncio.sleep(0.05)
                        recent_stderr = "".join(live.stderr_buffer)
                        bounded_stderr = _truncate_diag(recent_stderr, MAX_AUTH_DIAG_CHARS)
                        if live.closing and live.closed_reason in ("closed", "expired"):
                            err_msg = f"Session {live.closed_reason} during active turn"
                            finish_session_turn(
                                turn_id=turn_id,
                                status="failed",
                                stderr=bounded_stderr,
                                error=err_msg,
                                db_path=db_path,
                            )
                            raise HTTPException(
                                status_code=status.HTTP_409_CONFLICT,
                                detail=f"Session '{session_id}' is {live.closed_reason} and cannot accept new messages",
                            )
                        if live.auth_abort_reason or is_auth_error(recent_stderr):
                            reason_detail = (
                                live.auth_abort_reason
                                or _truncate_diag(recent_stderr)
                                or _truncate_diag(str(eof_err))
                            )
                            err_msg = _truncate_diag(
                                f"Agent authentication expired or missing ({live.agent}): {reason_detail}",
                                MAX_AUTH_DIAG_CHARS,
                            )
                            logger.error(f"Session {session_id} auth failure: {err_msg}")
                            finish_session_turn(
                                turn_id=turn_id,
                                status="failed",
                                stderr=bounded_stderr,
                                error=err_msg,
                                db_path=db_path,
                            )
                            live.status = "failed"
                            await self.close_session(session_id, reason="failed", error=err_msg, db_path=db_path)
                            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=err_msg)

                        stderr_tail = f" | stderr: {bounded_stderr}" if bounded_stderr else ""
                        err_msg = _truncate_diag(
                            f"Session process exited unexpectedly mid-turn: {eof_err}{stderr_tail}",
                            MAX_AUTH_DIAG_CHARS,
                        )
                        logger.error(f"Session {session_id} EOF: {err_msg}")
                        finish_session_turn(
                            turn_id=turn_id,
                            status="failed",
                            stderr=bounded_stderr,
                            error=err_msg,
                            db_path=db_path,
                        )
                        live.status = "failed"
                        await self.close_session(session_id, reason="failed", error=err_msg, db_path=db_path)
                        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=err_msg)
                    except asyncio.CancelledError:
                        raise
                    except Exception as stream_err:
                        recent_stderr = "".join(live.stderr_buffer)
                        bounded_stderr = _truncate_diag(recent_stderr, MAX_AUTH_DIAG_CHARS)
                        err_msg = _truncate_diag(f"Session stream protocol error: {stream_err}", MAX_AUTH_DIAG_CHARS)
                        logger.error(f"Session {session_id} stream parse failure: {err_msg}", exc_info=True)
                        finish_session_turn(
                            turn_id=turn_id,
                            status="failed",
                            stderr=bounded_stderr,
                            error=err_msg,
                            db_path=db_path,
                        )
                        live.status = "failed"
                        await self.close_session(session_id, reason="failed", error=err_msg, db_path=db_path)
                        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=err_msg)

                    response_text = result_data.get("response", "")
                    is_success = result_data.get("is_success", True)
                    raw_err_text = result_data.get("error")
                    err_text = _truncate_diag(raw_err_text, MAX_AUTH_DIAG_CHARS) if raw_err_text else None
                    recent_stderr = "".join(live.stderr_buffer)
                    bounded_stderr = _truncate_diag(recent_stderr, MAX_AUTH_DIAG_CHARS)

                    matched_pat = None
                    if response_text:
                        resp_lower = response_text.lower()
                        for pat in UNREADABLE_ATTACHMENT_PATTERNS:
                            if pat in resp_lower:
                                matched_pat = pat
                                break

                    if matched_pat:
                        is_success = False
                        err_text = _truncate_diag(
                            f"Output validation guard failed: agent reported missing attachment ('{matched_pat}')",
                            MAX_AUTH_DIAG_CHARS,
                        )
                        logger.error(f"Session {session_id} output validation guard failed: {err_text}")

                    # Rate limit check (NEVER regex-scan response payload!)
                    if not is_success:
                        if is_rate_limit_error(exit_code=1, stdout="", stderr=recent_stderr, error=raw_err_text or err_text):
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

                    turn_status = "completed" if is_success else "failed"
                    now_finish = time.time()
                    live.last_active_at = now_finish
                    session_open = not live.closing and live.status not in ("closed", "expired", "failed")
                    if session_open:
                        live.status = "idle"

                    try:
                        finished_turn, updated_session = complete_turn_and_update_session(
                            turn_id=turn_id,
                            turn_status=turn_status,
                            session_id=session_id,
                            response=response_text if is_success else None,
                            stderr=bounded_stderr,
                            turn_error=err_text,
                            turn_input_tokens=turn_usage.get("input_tokens"),
                            turn_output_tokens=turn_usage.get("output_tokens"),
                            turn_cached_tokens=turn_usage.get("cached_tokens"),
                            turn_total_tokens=turn_usage.get("total_tokens"),
                            turn_cost_usd=turn_cost,
                            session_status="idle" if session_open else None,
                            upstream_conversation_id=live.upstream_conversation_id,
                            turn_count=live.turn_count,
                            total_attachment_bytes=live.total_attachment_bytes,
                            cum_input_tokens=cum_usage.get("input_tokens"),
                            cum_output_tokens=cum_usage.get("output_tokens"),
                            cum_cached_tokens=cum_usage.get("cached_tokens"),
                            cum_total_tokens=cum_usage.get("total_tokens"),
                            cum_cost_usd=cum_cost,
                            session_error=(err_text if not is_success else None) if session_open else _UNSET,
                            last_active_at=now_finish if session_open else None,
                            db_path=db_path,
                        )
                    except Exception as persist_err:
                        raise await self._retire_on_db_failure(
                            live,
                            turn_id,
                            persist_err,
                            db_path=db_path,
                            prefix="Failed persisting turn result",
                        )

                    if not is_success:
                        if is_auth_error(raw_err_text, err_text, recent_stderr):
                            diag = _truncate_diag(raw_err_text or err_text or recent_stderr)
                            auth_detail = _truncate_diag(
                                f"Agent authentication expired or missing ({live.agent}): {diag}",
                                MAX_AUTH_DIAG_CHARS,
                            )
                            live.status = "failed"
                            await self.close_session(session_id, reason="failed", error=auth_detail, db_path=db_path)
                            raise HTTPException(
                                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                                detail=auth_detail,
                            )
                        await asyncio.sleep(0.02)
                        if live.proc.returncode is not None:
                            live.status = "failed"
                            await self.close_session(
                                session_id,
                                reason="failed",
                                error=err_text or "Session process exited on turn error",
                                db_path=db_path,
                            )
                        raise HTTPException(
                            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail=err_text or "Session turn failed",
                        )

                    return {
                        **(updated_session or {}),
                        "latest_turn": finished_turn,
                    }
                except asyncio.CancelledError:
                    if saved_this_turn:
                        _rollback_turn_uploads(live, saved_this_turn)
                        saved_this_turn.clear()
                        update_session_state(
                            session_id,
                            total_attachment_bytes=live.total_attachment_bytes,
                            db_path=db_path,
                        )
                    finish_session_turn(
                        turn_id=turn_id,
                        status="failed",
                        error=f"Turn cancelled ({live.closed_reason or 'cancelled'})",
                        db_path=db_path,
                    )
                    if live.closing or live.status in ("closed", "expired", "failed"):
                        raise HTTPException(
                            status_code=status.HTTP_409_CONFLICT,
                            detail=f"Session '{session_id}' is {live.closed_reason or live.status} and cannot accept new messages",
                        )
                    raise
                finally:
                    live.active_turn_task = None
        finally:
            if not decremented_waiter:
                live.queued_waiters = max(0, live.queued_waiters - 1)
                finish_session_turn(
                    turn_id=turn_id,
                    status="failed",
                    error="Turn cancelled while queued",
                    db_path=db_path,
                )

    async def _finalize_close(
        self,
        live: LiveSession,
        reason: str,
        error: Optional[str],
        api_key_name: Optional[str],
        db_path: Optional[str],
        caller_task: Optional[asyncio.Task] = None,
    ) -> Optional[Dict[str, Any]]:
        settings = get_settings()
        try:
            if live.drain_task and not live.drain_task.done():
                live.drain_task.cancel()

            clean_exit = False
            if reason in ("closed", "expired") and not live.lock.locked():
                try:
                    async def _graceful_eof_and_wait() -> None:
                        if live.proc.stdin and not live.proc.stdin.is_closing():
                            live.proc.stdin.close()
                            await live.proc.stdin.wait_closed()
                        await live.proc.wait()

                    await asyncio.wait_for(_graceful_eof_and_wait(), timeout=2.0)
                    clean_exit = True
                except Exception:
                    clean_exit = False

            if not clean_exit and live.proc.returncode is None:
                await _terminate_and_reap_proc(live.proc)

            active_task = live.active_turn_task
            if (
                active_task is not None
                and not active_task.done()
                and active_task is not caller_task
                and active_task is not asyncio.current_task()
            ):
                active_task.cancel()
                await asyncio.gather(active_task, return_exceptions=True)

            await cleanup_sandbox_job(live.id)
            if not (reason == "failed" and settings.keep_workspace_on_failure):
                try:
                    shutil.rmtree(live.workspace_path, ignore_errors=True)
                    shutil.rmtree(get_upload_staging_dir(live.workspace_path), ignore_errors=True)
                except Exception as exc:
                    logger.warning(f"Failed deleting session workspace {live.workspace_path}: {exc}")

            bounded_err = _truncate_diag(error, MAX_AUTH_DIAG_CHARS) if error else None
            try:
                closed_row = close_session_row(
                    session_id=live.id,
                    status=reason,
                    error=bounded_err,
                    db_path=db_path,
                    api_key_name=api_key_name,
                )
                existing = self._pending_db_reconcile.get(live.id)
                if existing is not None:
                    if existing[0] is not None:
                        # Winning close committed session status; keep only pending turn completion
                        self._pending_db_reconcile[live.id] = (existing[0], "", existing[2], existing[3])
                    else:
                        self._pending_db_reconcile.pop(live.id, None)
                return closed_row
            except Exception as db_exc:
                logger.warning(f"Failed writing terminal session row for '{live.id}' (queued for reconciliation): {db_exc}")
                existing = self._pending_db_reconcile.get(live.id)
                pending_turn_id = existing[0] if existing is not None else None
                self._pending_db_reconcile[live.id] = (pending_turn_id, reason, bounded_err, db_path)
                return None
        finally:
            async with self.lock:
                if live.proc.returncode is None:
                    self._unreaped_procs[live.id] = live.proc
                self._closing.pop(live.id, None)

    async def close_session(
        self,
        session_id: str,
        reason: str = "closed",
        error: Optional[str] = None,
        api_key_name: Optional[str] = None,
        db_path: Optional[str] = None,
        only_if_idle: bool = False,
    ) -> Optional[Dict[str, Any]]:
        existing_close_task: Optional[asyncio.Task] = None
        close_task: Optional[asyncio.Task] = None

        async with self.lock:
            live = self._sessions.get(session_id)
            if live is not None:
                if only_if_idle:
                    now = time.time()
                    if (
                        live.closing
                        or live.lock.locked()
                        or live.queued_waiters > 0
                        or live.status != "idle"
                        or (now - live.last_active_at) < live.idle_timeout
                    ):
                        return None
                self._sessions.pop(session_id, None)
                live.closing = True
                live.status = reason
                live.closed_reason = reason
                close_task = asyncio.create_task(
                    self._finalize_close(
                        live=live,
                        reason=reason,
                        error=error,
                        api_key_name=api_key_name,
                        db_path=db_path,
                        caller_task=asyncio.current_task(),
                    )
                )
                self._closing[session_id] = close_task
            else:
                existing_close_task = self._closing.get(session_id)

        if close_task is not None:
            return await asyncio.shield(close_task)

        if existing_close_task is not None:
            if existing_close_task is not asyncio.current_task():
                await asyncio.shield(existing_close_task)
            return get_session(session_id, db_path=db_path, api_key_name=api_key_name)

        if only_if_idle:
            return None

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


def sweep_orphaned_session_workspaces(
    work_root: Optional[str] = None,
    db_path: Optional[str] = None,
    min_age_seconds: float = 300.0,
) -> int:
    settings = get_settings()
    root = work_root or settings.session_work_root
    validate_distinct_work_roots(settings.work_root, root)
    if not os.path.exists(root):
        return 0

    now = time.time()
    reaped = 0

    for item in os.listdir(root):
        if item == ".staging":
            staging_root = os.path.join(root, ".staging")
            if os.path.isdir(staging_root):
                for sub_item in os.listdir(staging_root):
                    sub_dir = os.path.join(staging_root, sub_item)
                    if not os.path.isdir(sub_dir):
                        continue
                    try:
                        if (now - os.path.getmtime(sub_dir)) < min_age_seconds:
                            continue
                    except OSError:
                        continue
                    if session_manager.is_session_active(sub_item) or sub_item in session_manager._closing:
                        continue
                    sub_sess = get_session(sub_item, db_path=db_path)
                    if sub_sess is None or sub_sess.get("status") in ("closed", "expired", "failed"):
                        shutil.rmtree(sub_dir, ignore_errors=True)
            continue
        sess_dir = os.path.join(root, item)
        if not os.path.isdir(sess_dir):
            continue

        try:
            mtime = os.path.getmtime(sess_dir)
        except OSError:
            continue

        if (now - mtime) < min_age_seconds:
            continue

        if session_manager.is_session_active(item) or item in session_manager._closing:
            continue

        sess = get_session(item, db_path=db_path)
        if sess is None or sess.get("status") in ("closed", "expired", "failed"):
            try:
                shutil.rmtree(sess_dir)
                reaped += 1
            except Exception as err:
                logger.warning(f"Failed to sweep orphaned session workspace {sess_dir}: {err}")

    return reaped


session_manager = SessionManager()
