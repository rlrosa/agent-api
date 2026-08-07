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


def get_settings() -> Settings:


    api_key = os.environ.get("API_KEY")
    if not api_key:
        raise ValueError("API_KEY environment variable is required and cannot be empty")

    trusted_nets_raw = os.environ.get("TRUSTED_NETWORKS", "192.168.87.0/24,100.64.0.0/10")
    trusted_networks = [net.strip() for net in trusted_nets_raw.split(",") if net.strip()]

    return Settings(
        api_key=api_key,
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
    )






