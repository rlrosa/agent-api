import difflib
import logging
import shutil
from typing import Any, Callable, Dict, List, Optional
from pydantic import BaseModel, Field
from app.config import get_settings

logger = logging.getLogger("agent-api.agents")


class AgentSpec(BaseModel):
    name: str
    binary_name: str
    prompt_delivery: str  # "argv" or "stdin"
    supports_model_flag: bool
    default_model: Optional[str] = None
    default_flags: str = ""


# Note: The ordering of VALID_AGY_MODELS and VALID_CLAUDE_MODELS is load-bearing
# for cost safety. Ambiguous inputs scoring within 0.10 of the best fuzzy match
# resolve to whichever candidate appears earliest in these lists (cheapest-first).
VALID_AGY_MODELS = [
    "gemini-3.8-flash",
    "gemini-3.8-flash-low",
    "gemini-3.8-flash-medium",
    "gemini-3.8-flash-high",
    "gemini-4-argon",
    "gemini-4-argon-low",
    "gemini-4-argon-medium",
    "gemini-4-argon-high",
    "claude-sonnet-5-5",
    "claude-sonnet-5-5-low",
    "claude-sonnet-5-5-medium",
    "claude-sonnet-5-5-high",
    "claude-opus-5-5",
    "claude-opus-5-5-low",
    "claude-opus-5-5-medium",
    "claude-opus-5-5-high",
    "gpt-oss-120b",
    "gpt-oss-120b-medium",
]

LEGACY_AGY_MODEL_ALIASES = {
    "gemini-3.6-flash": "gemini-3.8-flash",
    "gemini-3.6-flash-low": "gemini-3.8-flash-low",
    "gemini-3.6-flash-medium": "gemini-3.8-flash-medium",
    "gemini-3.6-flash-high": "gemini-3.8-flash-high",
}

VALID_CLAUDE_MODELS = [
    "claude-haiku-4-5",
    "claude-sonnet-5",
    "claude-opus-4-8",
    "claude-opus-5",
]

VALID_EFFORTS = [
    "low",
    "medium",
    "high",
]


def resolve_model(agent: str, raw_model: Optional[str]) -> str:
    """
    Fuzzy resolves caller-supplied model string against supported model literals.
    Returns a safe, hardcoded literal string from the supported set.
    """
    settings = get_settings()
    if agent == "agy":
        supported = VALID_AGY_MODELS
        default = settings.agy_default_model or "gemini-3.8-flash-low"
    elif agent == "claude":
        supported = VALID_CLAUDE_MODELS
        default = settings.claude_default_model or "claude-sonnet-5"
    else:
        return "default"

    if default not in supported:
        default = supported[0]

    if not raw_model:
        return default

    raw = raw_model.strip()

    # Legacy back-compat alias mapping (applied BEFORE exact and fuzzy matching)
    if agent == "agy":
        for legacy_k, legacy_v in LEGACY_AGY_MODEL_ALIASES.items():
            if raw.lower() == legacy_k.lower():
                logger.info(f"Legacy agy model alias: requested '{raw}' -> aliased '{legacy_v}'")
                return legacy_v

    # Exact match check (case-insensitive)
    for s in supported:
        if s.lower() == raw.lower():
            return s

    # Fuzzy match with cheapest-wins tie-break (cutoff = 0.40)
    # Collect all candidates scoring >= 0.40, then pick the earliest in supported
    # list among those within 0.10 of the best score.
    scored = []
    raw_lower = raw.lower()
    for s in supported:
        score = difflib.SequenceMatcher(None, raw_lower, s.lower()).ratio()
        if score >= 0.40:
            scored.append((s, score))

    if scored:
        best_score = max(score for _, score in scored)
        threshold = best_score - 0.10
        for s in supported:
            for cand, score in scored:
                if cand == s and score >= threshold:
                    return cand

    # Safe fallback: no match or poor match lands on default model
    return default


def resolve_effort(raw_effort: Optional[str]) -> str:
    """
    Fuzzy resolves caller-supplied effort string against supported effort literals ('low', 'medium', 'high').
    Returns a safe, hardcoded literal string from VALID_EFFORTS.
    """
    if not raw_effort:
        return "low"

    raw = raw_effort.strip().lower()

    # Exact match check
    if raw in VALID_EFFORTS:
        return raw

    # Fuzzy match using stdlib difflib (cutoff = 0.4)
    matches = difflib.get_close_matches(raw, VALID_EFFORTS, n=1, cutoff=0.4)
    if matches:
        return matches[0]

    # Safe fallback: land on default effort 'low'
    return "low"


def validate_agent_model(agent: str, model: Optional[str]) -> str:
    return resolve_model(agent, model)


def get_agent_flags(agent_name: str) -> List[str]:
    settings = get_settings()
    if not settings.sandbox_enabled:
        return []

    if agent_name in settings.agent_flags:
        flags_str = settings.agent_flags[agent_name]
    else:
        spec = AGENTS.get(agent_name, {}).get("spec")
        flags_str = spec.default_flags if spec else ""

    return [f for f in flags_str.split() if f]


def strip_model_tier(model_name: str) -> str:
    for suffix in ("-low", "-medium", "-high"):
        if model_name.endswith(suffix):
            return model_name[:-len(suffix)]
    return model_name


def build_agy_argv(
    prompt: str,
    model: Optional[str] = None,
    effort: Optional[str] = None,
    workspace_path: Optional[str] = None,
) -> List[str]:
    spec = AGENTS["agy"]["spec"]
    cmd = ["agy", "-p", prompt]

    resolved_model = resolve_model("agy", model)
    if effort:
        base = strip_model_tier(resolved_model)
        resolved_eff = resolve_effort(effort)
        cmd.extend(["--model", base, "--effort", resolved_eff])
    else:
        cmd.extend(["--model", resolved_model])

    cmd.extend(get_agent_flags("agy"))

    if workspace_path:
        cmd.extend(["--add-dir", workspace_path])

    cmd.extend(["--output-format", "json"])
    return cmd


def build_claude_argv(
    prompt: str,
    model: Optional[str] = None,
    effort: Optional[str] = None,
    workspace_path: Optional[str] = None,
) -> List[str]:
    spec = AGENTS["claude"]["spec"]
    cmd = ["claude", "-p"]

    settings = get_settings()
    raw_model = model or settings.claude_default_model or spec.default_model

    if raw_model:
        resolved_model = resolve_model("claude", raw_model)
        cmd.extend(["--model", resolved_model])

    if effort:
        resolved_eff = resolve_effort(effort)
        cmd.extend(["--effort", resolved_eff])

    cmd.extend(get_agent_flags("claude"))

    cmd.extend(["--output-format", "json"])
    return cmd


def build_codex_argv(
    prompt: str,
    model: Optional[str] = None,
    effort: Optional[str] = None,
    workspace_path: Optional[str] = None,
) -> List[str]:
    raise NotImplementedError("codex agent is not installed on this system")



AGENTS: Dict[str, Dict] = {
    "agy": {
        "spec": AgentSpec(
            name="agy",
            binary_name="agy",
            prompt_delivery="argv",
            supports_model_flag=True,
            default_model=None,
            default_flags="--sandbox",
        ),
        "argv_builder": build_agy_argv,
    },
    "claude": {
        "spec": AgentSpec(
            name="claude",
            binary_name="claude",
            prompt_delivery="stdin",
            supports_model_flag=True,
            default_model=None,
            default_flags="--allowed-tools View,Read --permission-mode dontAsk",
        ),
        "argv_builder": build_claude_argv,
    },
    "codex": {
        "spec": AgentSpec(
            name="codex",
            binary_name="codex",
            prompt_delivery="argv",
            supports_model_flag=False,
            default_model=None,
            default_flags="",
        ),
        "argv_builder": build_codex_argv,
    },
    "mock_429": {
        "spec": AgentSpec(
            name="mock_429",
            binary_name="mock_429",
            prompt_delivery="argv",
            supports_model_flag=False,
            default_model=None,
            default_flags="",
        ),
        "argv_builder": None,
    },
}


def get_agent_availability() -> Dict[str, Dict[str, Any]]:
    result = {}
    settings = get_settings()

    for agent_name, info in AGENTS.items():
        if agent_name == "mock_429":
            result[agent_name] = {
                "available": settings.allow_mock_agent,
                "path": "/bin/mock_429" if settings.allow_mock_agent else None,
            }
            continue

        cmd = info["spec"].binary_name
        path = shutil.which(cmd)
        result[agent_name] = {
            "available": path is not None,
            "path": path,
        }
    return result


def ensure_available(agent_name: str) -> str:
    if agent_name not in AGENTS:
        raise ValueError(f"Unknown agent: '{agent_name}'")

    settings = get_settings()
    if agent_name == "mock_429":
        if not settings.allow_mock_agent:
            raise ValueError(f"Agent '{agent_name}' is unavailable (ALLOW_MOCK_AGENT disabled)")
        return "/bin/mock_429"

    cmd = AGENTS[agent_name]["spec"].binary_name
    path = shutil.which(cmd)
    if not path:
        raise ValueError(f"Agent '{agent_name}' (command '{cmd}') is not installed or not found on PATH")

    return path

