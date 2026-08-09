import os
from typing import List, Optional
from pydantic import BaseModel, Field



class Settings(BaseModel):
    api_key: str = Field(..., description="Shared secret for API key authentication")
    host: str = Field(default="0.0.0.0")
    port: int = Field(default=8090)
    trusted_networks: List[str] = Field(
        default_factory=lambda: ["192.168.87.0/24", "100.64.0.0/10"]
    )
    max_concurrency: int = Field(default=3)
    job_timeout: int = Field(default=120)
    wait_default: int = Field(default=60)
    wait_max: int = Field(default=90)
    db_path: str = Field(default="./data/jobs.db")
    work_root: str = Field(default="/var/tmp/agent-api/jobs")
    backoff_base: float = Field(default=5.0)
    backoff_max: float = Field(default=300.0)
    max_attempts: int = Field(default=5)
    max_attachment_bytes: int = Field(default=25 * 1024 * 1024)  # 25MB
    max_total_bytes: int = Field(default=90 * 1024 * 1024)       # 90MB
    agy_default_model: Optional[str] = Field(default=None)
    claude_default_model: Optional[str] = Field(default=None)
    default_agent: str = Field(default="agy")
    passthrough_env: str = Field(default="")
    keep_workspace_on_failure: bool = Field(default=False)
    sandbox_enabled: bool = Field(default=True)
    agy_sandbox_flags: str = Field(default="--sandbox")
    claude_disallowed_tools: str = Field(default="Bash,WebFetch")
    rate_limit_patterns: List[str] = Field(
        default_factory=lambda: [
            r"429",
            r"rate.?limit",
            r"RESOURCE_EXHAUSTED",
            r"QuotaFailure",
            r"quota",
            r"overloaded",
            r"too many requests",
            r"try again later",
        ]
    )
    recover_successes: int = Field(default=3)
    allow_mock_agent: bool = Field(default=False)
    bwrap_enabled: bool = Field(default=True)
    allow_unconfined: bool = Field(default=False)
    egress_restrict: bool = Field(default=False)
    api_keys: str = Field(default="")
    rate_limit_per_min: int = Field(default=60)
    max_pending_jobs: int = Field(default=100)
    log_level: str = Field(default="INFO")
    log_prompt_chars: int = Field(default=200)
    job_retention_days: int = Field(default=30)


def get_settings() -> Settings:

    api_key = os.environ.get("API_KEY", "")
    api_keys = os.environ.get("API_KEYS", "")
    if not api_key and not api_keys:
        raise ValueError("API_KEY or API_KEYS environment variable is required and cannot be empty")

    trusted_nets_raw = os.environ.get("TRUSTED_NETWORKS", "192.168.87.0/24,100.64.0.0/10")
    trusted_networks = [net.strip() for net in trusted_nets_raw.split(",") if net.strip()]

    # Parse JOB_RETENTION (e.g., "30d" or "30")
    retention_raw = os.environ.get("JOB_RETENTION", "30d").lower().replace("d", "").strip()
    try:
        job_retention_days = int(retention_raw)
    except ValueError:
        job_retention_days = 30

    return Settings(
        api_key=api_key or "default-secret-key",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8090")),
        trusted_networks=trusted_networks,
        max_concurrency=int(os.environ.get("MAX_CONCURRENCY", "3")),
        job_timeout=int(os.environ.get("JOB_TIMEOUT", "120")),
        wait_default=int(os.environ.get("WAIT_DEFAULT", "60")),
        wait_max=int(os.environ.get("WAIT_MAX", "90")),
        db_path=os.environ.get("DB_PATH", "./data/jobs.db"),
        work_root=os.environ.get("WORK_ROOT", "/var/tmp/agent-api/jobs"),
        backoff_base=float(os.environ.get("BACKOFF_BASE", "5.0")),
        backoff_max=float(os.environ.get("BACKOFF_MAX", "300.0")),
        max_attempts=int(os.environ.get("MAX_ATTEMPTS", "5")),
        max_attachment_bytes=int(os.environ.get("MAX_ATTACHMENT_BYTES", str(25 * 1024 * 1024))),
        max_total_bytes=int(os.environ.get("MAX_TOTAL_BYTES", str(90 * 1024 * 1024))),
        agy_default_model=os.environ.get("AGY_DEFAULT_MODEL"),
        claude_default_model=os.environ.get("CLAUDE_DEFAULT_MODEL"),
        default_agent=os.environ.get("DEFAULT_AGENT", "agy"),
        passthrough_env=os.environ.get("PASSTHROUGH_ENV", ""),
        keep_workspace_on_failure=os.environ.get("KEEP_WORKSPACE_ON_FAILURE", "0").lower() in ("1", "true", "yes"),
        sandbox_enabled=os.environ.get("SANDBOX_ENABLED", "1").lower() in ("1", "true", "yes"),
        agy_sandbox_flags=os.environ.get("AGY_SANDBOX_FLAGS", "--sandbox"),
        claude_disallowed_tools=os.environ.get("CLAUDE_DISALLOWED_TOOLS", "Bash,WebFetch"),
        recover_successes=int(os.environ.get("RECOVER_SUCCESSES", "3")),
        allow_mock_agent=os.environ.get("ALLOW_MOCK_AGENT", "0").lower() in ("1", "true", "yes"),
        bwrap_enabled=os.environ.get("BWRAP_ENABLED", "1").lower() in ("1", "true", "yes"),
        allow_unconfined=os.environ.get("ALLOW_UNCONFINED", "0").lower() in ("1", "true", "yes"),
        egress_restrict=os.environ.get("EGRESS_RESTRICT", "0").lower() in ("1", "true", "yes"),
        api_keys=os.environ.get("API_KEYS", ""),
        rate_limit_per_min=int(os.environ.get("RATE_LIMIT_PER_MIN", "60")),
        max_pending_jobs=int(os.environ.get("MAX_PENDING_JOBS", "100")),
        log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        log_prompt_chars=int(os.environ.get("LOG_PROMPT_CHARS", "200")),
        job_retention_days=job_retention_days,
    )








