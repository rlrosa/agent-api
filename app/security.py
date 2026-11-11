import os
import re
import secrets
import time
from typing import Dict, Optional, Set, Tuple
from app.config import get_settings

VALID_SCOPES: Set[str] = {"jobs", "stats", "admin"}
DEFAULT_SCOPES: Set[str] = {"jobs", "stats"}
ALL_SCOPES: Set[str] = {"jobs", "stats", "admin"}


class KeyInfo(str):
    """
    Represents an API key with associated name and scopes.
    Inherits from str so that keys_map[name] == secret remains True
    for backward compatibility with existing tests and callers.
    """
    secret: str
    scopes: Set[str]
    name: str

    def __new__(cls, secret: str, scopes: Set[str], name: str = ""):
        instance = super().__new__(cls, secret)
        instance.secret = secret
        instance.scopes = set(scopes)
        instance.name = name
        return instance

    def __eq__(self, other):
        if isinstance(other, str):
            return self.secret == str(other)
        if isinstance(other, KeyInfo):
            return self.secret == other.secret and self.scopes == other.scopes and self.name == other.name
        return False


class AuthContext(str):
    """
    Represents authenticated identity in request context.
    Inherits from str so that auth == "bypass" or passing auth as api_key_name
    remains completely compatible.
    """
    name: str
    scopes: Set[str]

    def __new__(cls, name: str, scopes: Set[str]):
        instance = super().__new__(cls, name)
        instance.name = name
        instance.scopes = set(scopes)
        return instance

    @property
    def key_name(self) -> str:
        return self.name

    def __eq__(self, other):
        if isinstance(other, str):
            return self.name == str(other)
        if isinstance(other, AuthContext):
            return self.name == other.name and self.scopes == other.scopes
        return False


def _parse_entry(entry: str) -> Optional[Tuple[str, str, Set[str]]]:
    """
    Parses a single API key entry.
    Format: 'name:key[:scope+scope]'

    Rule for colon in keys:
    If an entry has >= 3 colon-separated segments, the last segment is parsed
    as a scope suffix if and only if it matches ^[a-z+]+$ and all + separated
    parts are in VALID_SCOPES. Otherwise, the last segment is treated as part
    of the secret key itself, and the entry receives DEFAULT_SCOPES.
    """
    entry = entry.strip()
    if not entry or ":" not in entry:
        return None

    parts = entry.split(":")
    if len(parts) == 2:
        name, secret = parts[0].strip(), parts[1].strip()
        if not name or not secret:
            return None
        return name, secret, set(DEFAULT_SCOPES)

    # len(parts) >= 3
    name = parts[0].strip()
    last_segment = parts[-1].strip()

    if re.match(r"^[a-z+]+$", last_segment):
        candidate_scopes = set(last_segment.split("+"))
        if candidate_scopes and candidate_scopes.issubset(VALID_SCOPES):
            secret = ":".join(parts[1:-1]).strip()
            if not name or not secret:
                return None
            return name, secret, candidate_scopes

    # Last segment is not a valid scope list; treat everything after name as secret
    secret = ":".join(parts[1:]).strip()
    if not name or not secret:
        return None
    return name, secret, set(DEFAULT_SCOPES)


def parse_api_keys() -> Dict[str, KeyInfo]:
    settings = get_settings()
    keys_map: Dict[str, KeyInfo] = {}

    # Legacy single API_KEY
    if settings.api_key:
        secret = settings.api_key.strip()
        if secret:
            keys_map["default"] = KeyInfo(secret, scopes=set(DEFAULT_SCOPES), name="default")

    # Named API_KEYS (e.g. "laptop:key1,rondeau:key2:stats")
    if settings.api_keys:
        for entry in settings.api_keys.split(","):
            parsed = _parse_entry(entry)
            if parsed:
                name, secret, scopes = parsed
                keys_map[name] = KeyInfo(secret, scopes=scopes, name=name)

    return keys_map


def authenticate_key_info(provided_key: Optional[str]) -> Optional[KeyInfo]:
    if not provided_key:
        return None

    keys_map = parse_api_keys()
    cleaned = provided_key.strip()
    for name, info in keys_map.items():
        if secrets.compare_digest(cleaned, info.secret):
            return info
    return None


def authenticate_key(provided_key: Optional[str]) -> Optional[str]:
    info = authenticate_key_info(provided_key)
    return info.name if info else None


def get_key_scopes(key_name: str) -> Set[str]:
    if key_name == "bypass":
        return set(ALL_SCOPES)
    keys_map = parse_api_keys()
    if key_name in keys_map:
        return set(keys_map[key_name].scopes)
    return set()


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
