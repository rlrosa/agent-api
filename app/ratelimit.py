import asyncio
import logging
import random
import re
import time
from typing import Optional
from app.config import get_settings

logger = logging.getLogger("agent-api.ratelimit")


import json


def is_rate_limit_error(
    stdout: Optional[str] = None,
    stderr: Optional[str] = None,
    error: Optional[str] = None,
    exit_code: Optional[int] = None,
) -> bool:
    if exit_code == 429:
        return True

    # Layer 1: Governing Principle — exit_code 0 with non-empty stdout means SUCCESS.
    if exit_code == 0 and stdout and stdout.strip():
        return False

    # Layer 2 & 3: Structured JSON Envelope Inspection.
    # CRITICAL AUDIT RULE: NEVER regex-scan the `response` field in stdout.
    # The `response` field contains user payload (OCR text, item prices like "$ 429,00", street addresses like "AV ITALIA 4429", RUTs).
    stdout_envelope_text = ""
    if stdout and stdout.strip():
        try:
            parsed = json.loads(stdout.strip())
            if isinstance(parsed, dict):
                # Layer 2: If JSON envelope status is SUCCESS -> not rate-limited
                if parsed.get("status") == "SUCCESS":
                    return False
                # Layer 3: Strip `response` payload before inspecting envelope metadata
                envelope_copy = {k: v for k, v in parsed.items() if k != "response"}
                stdout_envelope_text = json.dumps(envelope_copy)
        except Exception:
            # Layer 5: Unparseable stdout (CLI crash path) -> do NOT use raw stdout; fall back to stderr/error scan
            stdout_envelope_text = ""

    # Layer 4: Apply rate limit regex patterns ONLY to stderr, error, and stdout envelope metadata (excluding response payload).
    text_to_check = f"{stdout_envelope_text}\n{stderr or ''}\n{error or ''}"
    if not text_to_check.strip():
        return False

    settings = get_settings()
    for pattern in settings.rate_limit_patterns:
        if re.search(pattern, text_to_check, re.IGNORECASE):
            return True

    return False




class RateLimitManager:
    def __init__(self):
        settings = get_settings()
        self.cooldown_until: float = 0.0
        self.effective_concurrency: int = settings.max_concurrency
        self.consecutive_successes: int = 0
        self._lock = asyncio.Lock()

    def is_in_cooldown(self) -> bool:
        return time.time() < self.cooldown_until

    def get_cooldown_remaining(self) -> float:
        return max(0.0, self.cooldown_until - time.time())

    async def handle_rate_limit(self, attempts: int) -> float:
        async with self._lock:
            settings = get_settings()
            # Calculate exponential backoff with jitter
            base_delay = min(
                settings.backoff_base * (2 ** (max(1, attempts) - 1)),
                settings.backoff_max,
            )
            jitter = random.uniform(0.5, 1.5)
            delay = base_delay * jitter

            now = time.time()
            self.cooldown_until = now + delay
            self.effective_concurrency = max(1, self.effective_concurrency // 2)
            self.consecutive_successes = 0

            logger.warning(
                f"Rate limit hit on attempt {attempts}! Delay: {delay:.2f}s. "
                f"Halved effective_concurrency to {self.effective_concurrency}. "
                f"Global cooldown until {self.cooldown_until:.2f}"
            )
            return delay

    async def handle_success(self) -> None:
        async with self._lock:
            settings = get_settings()
            self.consecutive_successes += 1
            if self.consecutive_successes >= settings.recover_successes:
                old = self.effective_concurrency
                self.effective_concurrency = min(
                    settings.max_concurrency, self.effective_concurrency + 1
                )
                self.consecutive_successes = 0
                if old != self.effective_concurrency:
                    logger.info(
                        f"AIMD Recovery: {settings.recover_successes} consecutive successes achieved. "
                        f"Increased effective_concurrency from {old} to {self.effective_concurrency}."
                    )

    def reset(self) -> None:
        settings = get_settings()
        self.cooldown_until = 0.0
        self.effective_concurrency = settings.max_concurrency
        self.consecutive_successes = 0


rate_limit_manager = RateLimitManager()
