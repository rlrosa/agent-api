import asyncio
import logging
import os
import re
import shlex
import shutil
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.agents import get_agent_flags
from app.config import get_settings

logger = logging.getLogger("agent-api.agy_quota")

_cache: Dict[str, Any] = {
    "data": None,
    "expires_at": 0.0,
}
_lock = asyncio.Lock()


def clear_quota_cache():
    """Clears the in-memory quota cache (used in tests)."""
    _cache["data"] = None
    _cache["expires_at"] = 0.0


def parse_agy_usage_tsv(stdout: Optional[str]) -> Optional[Dict[str, Any]]:
    """
    Parses tab-separated usage information from `agy -p /usage`.
    Expected format per line:
      <group>\t<bucket_name>\t<percent>%\t<reset_iso_timestamp>

    Example:
      Gemini Models\tWeekly Limit Remaining\t80%\t2026-11-14T06:29:14Z
      Gemini Models\tFive Hour Limit Remaining\t92%\t2026-11-09T03:29:14Z

    Returns structured dict with buckets list, or None if unparseable or empty.
    """
    if not stdout or not isinstance(stdout, str):
        return None

    lines = [l.strip() for l in stdout.strip().splitlines() if l.strip()]
    if not lines:
        return None

    buckets: List[Dict[str, Any]] = []

    for line in lines:
        parts = [p.strip() for p in line.split("\t")]
        if len(parts) < 4:
            return None

        group, bucket_name, percent_str, reset_iso = parts[0], parts[1], parts[2], parts[3]
        if not group or not bucket_name or not percent_str or not reset_iso:
            return None

        percent_match = re.match(r"^(\d{1,3})%$", percent_str)
        if not percent_match:
            return None

        percent = int(percent_match.group(1))
        if percent < 0 or percent > 100:
            return None

        # Parse reset_iso timestamp
        try:
            iso_clean = reset_iso.replace("Z", "+00:00")
            dt = datetime.fromisoformat(iso_clean)
            reset_at_str = dt.isoformat().replace("+00:00", "Z")
        except Exception:
            return None

        lower_bucket = bucket_name.lower()
        if "week" in lower_bucket:
            period = "weekly"
        elif "hour" in lower_bucket:
            period = "five_hour"
        else:
            period = "other"

        buckets.append({
            "group": group,
            "bucket": bucket_name,
            "period": period,
            "remaining_percent": percent,
            "reset_at": reset_at_str,
        })

    if not buckets:
        return None

    return {"buckets": buckets}


async def get_agy_quota(cmd_override: Optional[str] = None) -> Dict[str, Any]:
    """
    Retrieves current agy quota via subprocess, with TTL caching and single-flight lock.
    Does NOT create a job database record.
    """
    settings = get_settings()
    now = time.time()

    # Fast path: check cache before acquiring lock
    if _cache["data"] is not None and now < _cache["expires_at"]:
        return _cache["data"]

    async with _lock:
        now = time.time()
        if _cache["data"] is not None and now < _cache["expires_at"]:
            return _cache["data"]

        cmd_str = (cmd_override or settings.agy_usage_cmd).strip()
        fetched_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        try:
            parts = shlex.split(cmd_str)
            if not parts:
                raise ValueError("AGY_USAGE_CMD is empty")

            if parts[0] == "agy":
                binary = shutil.which("agy")
                if not binary:
                    raise FileNotFoundError("agy command not found in PATH")
                argv = [binary] + parts[1:]
                flags = get_agent_flags("agy")
                for f in flags:
                    if f not in argv:
                        argv.append(f)
            else:
                argv = list(parts)
                if not os.path.isabs(argv[0]) and os.path.exists(argv[0]):
                    argv[0] = os.path.abspath(argv[0])

            logger.info(f"Running agy quota command: {argv}")
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=30.0)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                raise TimeoutError("Command timed out after 30s")

            if proc.returncode != 0:
                err_text = stderr_bytes.decode("utf-8", errors="replace").strip()
                raise RuntimeError(err_text or f"Command exited with code {proc.returncode}")

            stdout_str = stdout_bytes.decode("utf-8", errors="replace")
            parsed = parse_agy_usage_tsv(stdout_str)
            if not parsed:
                raise ValueError("Unparseable agy usage TSV output")

            result = {
                "available": True,
                "fetched_at": fetched_at,
                "buckets": parsed["buckets"],
            }
            _cache["data"] = result
            _cache["expires_at"] = time.time() + float(settings.agy_quota_ttl_s)
            return result

        except Exception as err:
            logger.warning(f"Failed to fetch agy quota: {err}")
            result = {
                "available": False,
                "error": str(err),
                "fetched_at": fetched_at,
                "buckets": [],
            }
            _cache["data"] = result
            _cache["expires_at"] = time.time() + 60.0  # Cache failures briefly (60s)
            return result
