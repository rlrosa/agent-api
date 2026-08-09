import asyncio
import json
import logging
import os
import shutil
import signal
from typing import Any, Dict, List, Optional
from app.agents import AGENTS, ensure_available
from app.config import get_settings
from app.db import finish_job


logger = logging.getLogger("agent-api.runner")
ACTIVE_PROCESSES: Dict[str, asyncio.subprocess.Process] = {}



def kill_active_job_process(job_id: str) -> bool:
    proc = ACTIVE_PROCESSES.get(job_id)
    if proc and proc.returncode is None:
        try:
            try:
                proc.kill()
            except Exception:
                pass
            pgid = os.getpgid(proc.pid)
            try:
                os.killpg(pgid, signal.SIGKILL)
            except Exception:
                pass
            for pipe in (proc.stdout, proc.stderr, proc.stdin):
                if pipe is not None:
                    try:
                        pipe.close()
                    except Exception:
                        pass
            return True
        except Exception:
            pass
    return False





def build_scrubbed_env(settings) -> Dict[str, str]:
    allowed_keys = {"PATH", "HOME"}
    if settings.passthrough_env:
        for name in settings.passthrough_env.split(","):
            name = name.strip()
            if name:
                allowed_keys.add(name)

    scrubbed = {}
    for k, v in os.environ.items():
        if k in allowed_keys:
            scrubbed[k] = v

    if "PATH" not in scrubbed and "PATH" in os.environ:
        scrubbed["PATH"] = os.environ["PATH"]
    if "HOME" not in scrubbed and "HOME" in os.environ:
        scrubbed["HOME"] = os.environ["HOME"]

    return scrubbed


def check_bwrap_confinement() -> Dict[str, Any]:
    settings = get_settings()
    if not settings.bwrap_enabled:
        return {"enabled": False, "available": False, "mode": "disabled"}

    bwrap_path = shutil.which("bwrap")
    if not bwrap_path:
        mode = "unconfined_warning" if settings.allow_unconfined else "enforced"
        return {"enabled": True, "available": False, "mode": mode}

    return {"enabled": True, "available": True, "mode": "enforced"}


def wrap_cmd_with_bwrap(cmd: List[str], workspace_path: str, agent: str) -> List[str]:
    settings = get_settings()
    if not settings.bwrap_enabled:
        return cmd

    bwrap_path = shutil.which("bwrap")
    if not bwrap_path:
        if not settings.allow_unconfined:
            raise RuntimeError("Confinement error: bubblewrap (bwrap) binary missing or unexecutable, refusing unconfined execution")
        logger.warning(f"UNCONFINED EXECUTION WARNING: bwrap missing/disabled, executing unconfined due to ALLOW_UNCONFINED=1 for agent '{agent}'")
        return cmd


    bwrap_cmd = [
        bwrap_path,
        "--die-with-parent",
        "--new-session",
        "--dir", "/etc",
        "--ro-bind", "/usr", "/usr",
        "--ro-bind", "/bin", "/bin",
        "--ro-bind", "/sbin", "/sbin",
        "--ro-bind", "/lib", "/lib",
    ]

    if os.path.exists("/lib64"):
        bwrap_cmd.extend(["--ro-bind", "/lib64", "/lib64"])

    for etc_item in ["resolv.conf", "ssl", "ca-certificates", "hosts", "nsswitch.conf", "gai.conf"]:
        path = os.path.join("/etc", etc_item)
        if os.path.exists(path):
            bwrap_cmd.extend(["--ro-bind", path, path])

    bwrap_cmd.extend([
        "--dev", "/dev",
        "--proc", "/proc",
        "--share-net",
        "--tmpfs", "/tmp",
        "--tmpfs", "/home/ubuntu",
        "--ro-bind", "/home/ubuntu/.local", "/home/ubuntu/.local",
    ])

    if agent == "agy":
        if os.path.exists("/home/ubuntu/.gemini"):
            bwrap_cmd.extend([
                "--ro-bind", "/home/ubuntu/.gemini", "/home/ubuntu/.gemini",
                "--tmpfs", "/home/ubuntu/.gemini/antigravity-cli/brain",
                "--tmpfs", "/home/ubuntu/.gemini/antigravity-cli/conversations",
                "--tmpfs", "/home/ubuntu/.gemini/antigravity-cli/cache",
                "--tmpfs", "/home/ubuntu/.gemini/antigravity-cli/log",
            ])
    elif agent == "claude":
        bwrap_cmd.extend(["--tmpfs", "/home/ubuntu/.claude"])
        if os.path.exists("/home/ubuntu/.claude/.credentials.json"):
            bwrap_cmd.extend([
                "--ro-bind", "/home/ubuntu/.claude/.credentials.json", "/home/ubuntu/.claude/.credentials.json",
            ])
        if os.path.exists("/home/ubuntu/.claude/settings.json"):
            bwrap_cmd.extend([
                "--ro-bind", "/home/ubuntu/.claude/settings.json", "/home/ubuntu/.claude/settings.json",
            ])
        if os.path.exists("/home/ubuntu/.claude.json"):
            bwrap_cmd.extend([
                "--ro-bind", "/home/ubuntu/.claude.json", "/home/ubuntu/.claude.json",
            ])





    bwrap_cmd.extend([
        "--bind", workspace_path, workspace_path,
        "--chdir", workspace_path,
    ])
    bwrap_cmd.extend(cmd)
    return bwrap_cmd



async def run_job(job: Dict[str, Any], custom_argv: Optional[List[str]] = None) -> Dict[str, Any]:
    settings = get_settings()
    job_id = job["id"]
    agent_name = job["agent"]

    work_root = settings.work_root
    workspace_path = os.path.join(work_root, job_id)
    attachments_path = os.path.join(workspace_path, "attachments")

    os.makedirs(attachments_path, exist_ok=True)

    env = build_scrubbed_env(settings)
    prompt = job["prompt"]

    if custom_argv is not None:
        argv = wrap_cmd_with_bwrap(custom_argv, workspace_path, agent_name)
        stdin_bytes = None
    else:
        if job["agent"] == "mock_429":
            return finish_job(
                job_id,
                status="failed",
                exit_code=429,
                stderr="HTTP 429 Too Many Requests: Rate limit quota exceeded. Please try again later.",
                error="HTTP 429 Too Many Requests: Rate limit quota exceeded. Please try again later.",
            )
        ensure_available(agent_name)
        agent_data = AGENTS[agent_name]
        spec = agent_data["spec"]
        builder = agent_data["argv_builder"]

        raw_argv = builder(
            prompt,
            model=job.get("model"),
            effort=job.get("effort"),
            workspace_path=workspace_path,
        )

        try:
            argv = wrap_cmd_with_bwrap(raw_argv, workspace_path, agent_name)
        except RuntimeError as conf_err:
            logger.error(f"Job {job_id} confinement error: {conf_err}")
            return finish_job(
                job_id=job_id,
                status="failed",
                exit_code=-1,
                error=str(conf_err),
                db_path=job.get("db_path"),
            )



        if spec.prompt_delivery == "stdin":
            stdin_bytes = prompt.encode("utf-8")
        else:
            stdin_bytes = None


    timeout = job.get("timeout") or settings.job_timeout
    status = "failed"
    exit_code = None
    stdout_str = None
    stderr_str = None
    error_msg = None
    input_tokens = None
    output_tokens = None
    cached_tokens = None
    total_tokens = None
    cost_usd = None

    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=workspace_path,
            env=env,
            stdin=asyncio.subprocess.PIPE if stdin_bytes is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        ACTIVE_PROCESSES[job_id] = proc

        stdout_data, stderr_data = await asyncio.wait_for(
            proc.communicate(input=stdin_bytes),
            timeout=float(timeout),
        )

        stdout_str = stdout_data.decode("utf-8", errors="replace")
        stderr_str = stderr_data.decode("utf-8", errors="replace")
        exit_code = proc.returncode


        if exit_code == 0:
            status = "completed"
            error_msg = None
            try:
                parsed = json.loads(stdout_str)
                if isinstance(parsed, dict):
                    if agent_name == "agy":
                        answer = parsed.get("response")
                        if answer is not None:
                            stdout_str = str(answer)
                        usage = parsed.get("usage") or {}
                        input_tokens = usage.get("input_tokens")
                        output_tokens = usage.get("output_tokens")
                        cached_tokens = usage.get("cache_read_tokens") or usage.get("cache_read_input_tokens")
                        total_tokens = usage.get("total_tokens") or (
                            (input_tokens or 0) + (output_tokens or 0)
                        )
                        cost_usd = None
                    elif agent_name == "claude":
                        answer = parsed.get("result")
                        if answer is not None:
                            stdout_str = str(answer)
                        usage = parsed.get("usage") or {}
                        input_tokens = usage.get("input_tokens")
                        output_tokens = usage.get("output_tokens")
                        cached_tokens = usage.get("cache_read_input_tokens") or usage.get("cache_read_tokens")
                        total_tokens = (input_tokens or 0) + (output_tokens or 0)
                        if cached_tokens:
                            total_tokens += cached_tokens
                        cost_usd = parsed.get("total_cost_usd")
                logger.info(
                    f"Job {job_id} parsed tokens for agent {agent_name}: input={input_tokens}, output={output_tokens}, "
                    f"total={total_tokens}, cost={cost_usd}, new_stdout_len={len(stdout_str)}"
                )
            except Exception as parse_err:
                logger.warning(f"Job {job_id} stdout JSON parse fallback: {parse_err}")

        else:
            status = "failed"
            error_msg = stderr_str.strip() if stderr_str.strip() else f"Process exited with exit code {exit_code}"

    except asyncio.TimeoutError:
        status = "failed"
        error_msg = f"Job timed out after {timeout}s"
        exit_code = -1

        if proc is not None:
            pid = proc.pid
            try:
                os.killpg(pid, signal.SIGTERM)
            except Exception:
                pass
            try:
                proc.terminate()
            except Exception:
                pass

            await asyncio.sleep(0.2)

            try:
                os.killpg(pid, signal.SIGKILL)
            except Exception:
                pass
            try:
                proc.kill()
            except Exception:
                pass

            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except Exception:
                pass

    except Exception as exc:
        status = "failed"
        error_msg = str(exc)
        exit_code = -1

    finally:
        ACTIVE_PROCESSES.pop(job_id, None)
        should_keep = settings.keep_workspace_on_failure and status == "failed"
        if not should_keep:
            if os.path.exists(workspace_path):
                shutil.rmtree(workspace_path, ignore_errors=True)

    result = finish_job(
        job_id=job_id,
        status=status,
        exit_code=exit_code,
        stdout=stdout_str,
        stderr=stderr_str,
        error=error_msg,
        finished_at=None,
        db_path=job.get("db_path"),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached_tokens,
        total_tokens=total_tokens,
        cost_usd=cost_usd,
    )
    return result


