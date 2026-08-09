import os
import secrets
import time
from typing import Dict, Optional, Tuple
from app.config import get_settings


def parse_api_keys() -> Dict[str, str]:
    settings = get_settings()
    keys_map: Dict[str, str] = {}

    # Legacy single API_KEY
    if settings.api_key:
        keys_map["default"] = settings.api_key.strip()

    # Named API_KEYS (e.g. "laptop:key1,rondeau:key2")
    if settings.api_keys:
        for pair in settings.api_keys.split(","):
            pair = pair.strip()
            if ":" in pair:
                name, key_val = pair.split(":", 1)
                name = name.strip()
                key_val = key_val.strip()
                if name and key_val:
                    keys_map[name] = key_val

    return keys_map


def authenticate_key(provided_key: Optional[str]) -> Optional[str]:
    if not provided_key:
        return None

    keys_map = parse_api_keys()
    for name, secret in keys_map.items():
        if secrets.compare_digest(provided_key.strip(), secret):
            return name
    return None


class SlidingWindowRateLimiter:
    """In-memory sliding-window rate limiter per caller."""
    def __init__(self):
        self.history: Dict[str, list] = {}

    def is_rate_limited(self, caller_id: str, limit_per_min: int) -> Tuple[bool, int]:
        if limit_per_min <= 0:
            return False, 0

        now = time.time()
        window_start = now - 60.0

        if caller_id not in self.history:
            self.history[caller_id] = []

        # Remove entries older than window
        timestamps = [ts for ts in self.history[caller_id] if ts > window_start]
        self.history[caller_id] = timestamps

        if len(timestamps) >= limit_per_min:
            oldest = timestamps[0]
            retry_after = int(max(1, 60.0 - (now - oldest)))
            return True, retry_after

        self.history[caller_id].append(now)
        return False, 0


rate_limiter = SlidingWindowRateLimiter()
