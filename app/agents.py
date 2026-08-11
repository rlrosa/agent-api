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


VALID_AGY_MODELS = [
    "gemini-3.6-flash",
    "gemini-3.6-flash-low",
    "gemini-3.6-flash-medium",
    "gemini-3.6-flash-high",
]

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
        default = settings.agy_default_model or "gemini-3.6-flash"
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

    # Exact match check (case-insensitive)
    for s in supported:
        if s.lower() == raw.lower():
            return s

    # Fuzzy match using stdlib difflib (cutoff = 0.4 for reasonable similarity)
    matches = difflib.get_close_matches(raw.lower(), [s.lower() for s in supported], n=1, cutoff=0.4)
    if matches:
        matched_lower = matches[0]
        for s in supported:
            if s.lower() == matched_lower:
                return s

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


def build_agy_argv(
    prompt: str,
    model: Optional[str] = None,
    effort: Optional[str] = None,
    workspace_path: Optional[str] = None,
) -> List[str]:
    spec = AGENTS["agy"]["spec"]
    cmd = ["agy", "-p", prompt]

    eff = resolve_effort(effort) if effort else "low"
    selected_model = f"gemini-3.6-flash-{eff}"

    cmd.extend(["--model", selected_model])
    if effort:
        cmd.extend(["--effort", eff])

    settings = get_settings()
    if settings.sandbox_enabled and settings.agy_sandbox_flags:
        for flag in settings.agy_sandbox_flags.split():
            if flag:
                cmd.append(flag)

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

    if settings.sandbox_enabled:
        cmd.extend(["--allowed-tools", "View,Read"])
        cmd.extend(["--permission-mode", "dontAsk"])

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

