import asyncio
import os
import shutil
import signal
from typing import Any, Dict, List, Optional
from app.agents import AGENTS, ensure_available
from app.config import get_settings
from app.db import finish_job

ACTIVE_PROCESSES: Dict[str, asyncio.subprocess.Process] = {}


def kill_active_job_process(job_id: str) -> bool:
    proc = ACTIVE_PROCESSES.get(job_id)
    if proc and proc.returncode is None:
        try:
            pgid = os.getpgid(proc.pid)
            try:
                os.killpg(pgid, signal.SIGTERM)
            except Exception:
                pass
            try:
                os.killpg(pgid, signal.SIGKILL)
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


def wrap_cmd_with_bwrap(cmd: List[str], workspace_path: str, agent: str) -> List[str]:
    settings = get_settings()
    if not settings.bwrap_enabled:
        return cmd

    bwrap_path = shutil.which("bwrap")
    if not bwrap_path:
        return cmd

    bwrap_cmd = [
        bwrap_path,
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
        if os.path.exists("/home/ubuntu/.claude"):
            bwrap_cmd.extend([
                "--ro-bind", "/home/ubuntu/.claude", "/home/ubuntu/.claude",
                "--tmpfs", "/home/ubuntu/.claude/session-env",
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

        argv = wrap_cmd_with_bwrap(raw_argv, workspace_path, agent_name)


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
        else:
            status = "failed"
            error_msg = stderr_str.strip() if stderr_str.strip() else f"Process exited with exit code {exit_code}"

    except asyncio.TimeoutError:
        status = "failed"
        error_msg = f"Job timed out after {timeout}s"
        exit_code = -1

        if proc is not None:
            try:
                pgid = os.getpgid(proc.pid)
                os.killpg(pgid, signal.SIGTERM)
                await asyncio.sleep(0.5)
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            except ProcessLookupError:
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
    )
    return result
