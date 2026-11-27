import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
from typing import Any, Dict, List, Optional

from app.config import Settings, get_settings

logger = logging.getLogger("agent-api.sandbox")

# Allowlist of files/dirs inside ~/.gemini/antigravity-cli that agy needs for OAuth auth & config.
# Everything else (history.jsonl, conversation_summaries.db*, jetbox_summaries_proto.pb,
# conversations/, brain/, scratch/, presence/, implicit/, annotations/) is excluded (F5 mitigation).
AGY_ALLOWED_CLI_ITEMS = (
    "antigravity-oauth-token",
    "settings.json",
    "installation_id",
    "builtin",
)

AGY_WRITABLE_CLI_SUBDIRS = (
    "brain",
    "conversations",
    "cache",
    "log",
)

CLAUDE_ALLOWED_DIR_ITEMS = (
    ".credentials.json",
    "settings.json",
)

_OPENSHELL_BANNER_LINE_RE = re.compile(
    r"^(?:"
    r"Created sandbox:\s+\S+|"
    r"\[\d+(?:\.\d+)?s\]\s+.*|"
    r"[✓✔]\s+(?:Sandbox allocated|Image already present|Deleted sandbox)\b.*|"
    r"[⠁-⣿]\s+.*"
    r")$"
)


def sanitize_sandbox_name(raw_id: Optional[str], workspace_path: str) -> str:
    """
    Produces a deterministic RFC-1123 sandbox name of at most 19 characters
    (OpenShell v0.1.10 enforces a 19-char maximum on sandbox names).
    """
    candidate = raw_id or os.path.basename(os.path.normpath(workspace_path)) or "job"
    clean = re.sub(r"[^a-zA-Z0-9-]", "-", str(candidate)).strip("-").lower()
    clean = re.sub(r"-+", "-", clean)
    if not clean:
        clean = "job"
    if len(clean) <= 14:
        return f"aapi-{clean}"
    alnum = re.sub(r"[^a-z0-9]", "", clean)
    digest = hashlib.sha256(clean.encode("utf-8")).hexdigest()[:5]
    prefix = (alnum[:8] or "job").ljust(8, "0")
    return f"aapi-{prefix}-{digest}"


def strip_openshell_banner(stdout_str: Optional[str]) -> Optional[str]:
    """
    Strips OpenShell CLI provisioning progress/spinner lines from stdout
    so downstream JSON parsers and job outputs receive only the workload stdout.
    """
    if not stdout_str:
        return stdout_str

    cleaned_lines: List[str] = []
    for raw_line in stdout_str.splitlines():
        # Strip carriage-return spinner overwrites within a single line
        segments = [seg.strip() for seg in raw_line.split("\r") if seg.strip()]
        if not segments:
            continue
        kept_segments = [
            seg for seg in segments if not _OPENSHELL_BANNER_LINE_RE.match(seg)
        ]
        if kept_segments:
            cleaned_lines.append(kept_segments[-1])

    return "\n".join(cleaned_lines)


class NoopSandboxBackend:
    name = "none"

    def __init__(self, settings: Settings):
        self.settings = settings

    def check_confinement(self) -> Dict[str, Any]:
        return {
            "enabled": False,
            "available": False,
            "backend": "none",
            "mode": "disabled",
        }

    def wrap_command(
        self,
        cmd: List[str],
        workspace_path: str,
        agent: str,
        job_id: Optional[str] = None,
    ) -> List[str]:
        return cmd

    def clean_stdout(self, stdout_str: Optional[str]) -> Optional[str]:
        return stdout_str

    async def cleanup_job(self, job_id: str) -> None:
        return None


class BwrapBackend:
    name = "bwrap"

    def __init__(self, settings: Settings):
        self.settings = settings

    def check_confinement(self) -> Dict[str, Any]:
        bwrap_path = shutil.which("bwrap")
        if not bwrap_path:
            mode = "unconfined_warning" if self.settings.allow_unconfined else "enforced"
            return {
                "enabled": True,
                "available": False,
                "backend": "bwrap",
                "mode": mode,
            }
        return {
            "enabled": True,
            "available": True,
            "backend": "bwrap",
            "mode": "enforced",
        }

    def wrap_command(
        self,
        cmd: List[str],
        workspace_path: str,
        agent: str,
        job_id: Optional[str] = None,
    ) -> List[str]:
        bwrap_path = shutil.which("bwrap")
        if not bwrap_path:
            if not self.settings.allow_unconfined:
                raise RuntimeError(
                    "Confinement error: bubblewrap (bwrap) binary missing or unexecutable, refusing unconfined execution"
                )
            logger.warning(
                f"UNCONFINED EXECUTION WARNING: bwrap missing/disabled, executing unconfined due to ALLOW_UNCONFINED=1 for agent '{agent}'"
            )
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

        home_dir = os.environ.get("HOME") or os.path.expanduser("~")
        net_flag = "--unshare-net" if self.settings.egress_restrict else "--share-net"
        bwrap_cmd.extend([
            "--dev", "/dev",
            "--proc", "/proc",
            net_flag,
            "--tmpfs", "/tmp",
            "--tmpfs", home_dir,
        ])

        local_dir = os.path.join(home_dir, ".local")
        if os.path.exists(local_dir):
            bwrap_cmd.extend(["--ro-bind", local_dir, local_dir])

        if agent == "agy":
            gemini_dir = os.path.join(home_dir, ".gemini")
            cli_dir = os.path.join(gemini_dir, "antigravity-cli")
            bwrap_cmd.extend([
                "--tmpfs", gemini_dir,
                "--dir", cli_dir,
            ])
            for item in AGY_ALLOWED_CLI_ITEMS:
                src = os.path.join(cli_dir, item)
                if os.path.exists(src):
                    bwrap_cmd.extend(["--ro-bind", src, src])
            for sub in AGY_WRITABLE_CLI_SUBDIRS:
                bwrap_cmd.extend(["--dir", os.path.join(cli_dir, sub)])

            gemini_config_dir = os.path.join(gemini_dir, "config")
            if os.path.exists(gemini_config_dir):
                bwrap_cmd.extend(["--ro-bind", gemini_config_dir, gemini_config_dir])

            antigravity_config = os.path.join(home_dir, ".config", "antigravity")
            if os.path.exists(antigravity_config):
                bwrap_cmd.extend(["--ro-bind", antigravity_config, antigravity_config])

        elif agent == "claude":
            claude_dir = os.path.join(home_dir, ".claude")
            bwrap_cmd.extend(["--tmpfs", claude_dir])
            for item in CLAUDE_ALLOWED_DIR_ITEMS:
                src = os.path.join(claude_dir, item)
                if os.path.exists(src):
                    bwrap_cmd.extend(["--ro-bind", src, src])
            claude_json = os.path.join(home_dir, ".claude.json")
            if os.path.exists(claude_json):
                bwrap_cmd.extend(["--ro-bind", claude_json, claude_json])

        elif self.settings.allow_mock_agent and agent in ("mock_agy_session", "mock_claude_session"):
            import sys
            mock_script = os.path.abspath(
                os.path.join(os.path.dirname(__file__), "..", "tests", "fixtures", "mock_stream_agent.py")
            )
            if os.path.exists(mock_script):
                bwrap_cmd.extend(["--ro-bind", mock_script, mock_script])
            venv_prefix = os.path.abspath(sys.prefix)
            if os.path.exists(venv_prefix) and not venv_prefix.startswith(("/usr/", "/bin/", "/lib/")):
                bwrap_cmd.extend(["--ro-bind", venv_prefix, venv_prefix])

        bwrap_cmd.extend([
            "--bind", workspace_path, workspace_path,
            "--chdir", workspace_path,
        ])
        bwrap_cmd.extend(cmd)
        return bwrap_cmd

    def clean_stdout(self, stdout_str: Optional[str]) -> Optional[str]:
        return stdout_str

    async def cleanup_job(self, job_id: str) -> None:
        return None


class OpenShellBackend:
    name = "openshell"

    def __init__(self, settings: Settings):
        self.settings = settings

    def check_confinement(self) -> Dict[str, Any]:
        openshell_path = shutil.which("openshell")
        if not openshell_path:
            mode = "unconfined_warning" if self.settings.allow_unconfined else "enforced"
            return {
                "enabled": True,
                "available": False,
                "backend": "openshell",
                "mode": mode,
            }
        return {
            "enabled": True,
            "available": True,
            "backend": "openshell",
            "mode": "enforced",
        }

    def _build_mounts(self, workspace_path: str, agent: str) -> List[Dict[str, Any]]:
        mounts: List[Dict[str, Any]] = [
            {
                "type": "bind",
                "source": os.path.abspath(workspace_path),
                "target": os.path.abspath(workspace_path),
                "read_only": False,
            }
        ]
        home_dir = os.environ.get("HOME") or os.path.expanduser("~")

        if agent in ("agy", "mock_agy_session"):
            gemini_dir = os.path.join(home_dir, ".gemini")
            cli_dir = os.path.join(gemini_dir, "antigravity-cli")
            for item in AGY_ALLOWED_CLI_ITEMS:
                src = os.path.join(cli_dir, item)
                if os.path.exists(src):
                    mounts.append({
                        "type": "bind",
                        "source": src,
                        "target": src,
                        "read_only": True,
                    })
            gemini_config_dir = os.path.join(gemini_dir, "config")
            if os.path.exists(gemini_config_dir):
                mounts.append({
                    "type": "bind",
                    "source": gemini_config_dir,
                    "target": gemini_config_dir,
                    "read_only": True,
                })
            antigravity_config = os.path.join(home_dir, ".config", "antigravity")
            if os.path.exists(antigravity_config):
                mounts.append({
                    "type": "bind",
                    "source": antigravity_config,
                    "target": antigravity_config,
                    "read_only": True,
                })
        elif agent in ("claude", "mock_claude_session"):
            claude_dir = os.path.join(home_dir, ".claude")
            for item in CLAUDE_ALLOWED_DIR_ITEMS:
                src = os.path.join(claude_dir, item)
                if os.path.exists(src):
                    mounts.append({
                        "type": "bind",
                        "source": src,
                        "target": src,
                        "read_only": True,
                    })
            claude_json = os.path.join(home_dir, ".claude.json")
            if os.path.exists(claude_json):
                mounts.append({
                    "type": "bind",
                    "source": claude_json,
                    "target": claude_json,
                    "read_only": True,
                })

        return mounts

    def wrap_command(
        self,
        cmd: List[str],
        workspace_path: str,
        agent: str,
        job_id: Optional[str] = None,
    ) -> List[str]:
        openshell_path = shutil.which("openshell")
        if not openshell_path:
            if not self.settings.allow_unconfined:
                raise RuntimeError(
                    "Confinement error: openshell binary missing or unexecutable, refusing unconfined execution"
                )
            logger.warning(
                f"UNCONFINED EXECUTION WARNING: openshell missing/disabled, executing unconfined due to ALLOW_UNCONFINED=1 for agent '{agent}'"
            )
            return cmd

        sandbox_name = sanitize_sandbox_name(job_id, workspace_path)
        policy_path = os.path.abspath(self.settings.openshell_policy_path)
        mounts = self._build_mounts(workspace_path, agent)
        driver_config = {
            "docker": {
                "mounts": mounts,
            }
        }

        openshell_cmd = [
            openshell_path,
            "sandbox",
            "create",
            "--name",
            sandbox_name,
            "--from",
            self.settings.openshell_image,
            "--policy",
            policy_path,
            "--no-tty",
            "--driver-config-json",
            json.dumps(driver_config, separators=(",", ":")),
            "--",
            *cmd,
        ]
        return openshell_cmd

    def clean_stdout(self, stdout_str: Optional[str]) -> Optional[str]:
        return strip_openshell_banner(stdout_str)

    async def cleanup_job(self, job_id: str) -> None:
        if not job_id:
            return
        openshell_path = shutil.which("openshell")
        if not openshell_path:
            return

        sandbox_name = sanitize_sandbox_name(job_id, "")
        try:
            proc = await asyncio.create_subprocess_exec(
                openshell_path,
                "sandbox",
                "delete",
                sandbox_name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=15.0)
        except Exception as exc:
            logger.debug(f"OpenShell sandbox cleanup for '{sandbox_name}' finished with note: {exc}")


def get_sandbox_backend(settings: Optional[Settings] = None):
    cfg = settings or get_settings()
    if not cfg.bwrap_enabled or cfg.sandbox_backend == "none":
        return NoopSandboxBackend(cfg)
    if cfg.sandbox_backend == "openshell":
        return OpenShellBackend(cfg)
    return BwrapBackend(cfg)


def check_confinement() -> Dict[str, Any]:
    return get_sandbox_backend().check_confinement()


def wrap_command(
    cmd: List[str],
    workspace_path: str,
    agent: str,
    job_id: Optional[str] = None,
) -> List[str]:
    return get_sandbox_backend().wrap_command(
        cmd=cmd,
        workspace_path=workspace_path,
        agent=agent,
        job_id=job_id,
    )


def clean_sandbox_stdout(stdout_str: Optional[str]) -> Optional[str]:
    return get_sandbox_backend().clean_stdout(stdout_str)


async def cleanup_sandbox_job(job_id: str) -> None:
    await get_sandbox_backend().cleanup_job(job_id)
