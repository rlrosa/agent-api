import shutil
from typing import Any, Callable, Dict, List, Optional
from pydantic import BaseModel, Field
from app.config import get_settings


class AgentSpec(BaseModel):
    name: str
    binary_name: str
    prompt_delivery: str  # "argv" or "stdin"
    supports_model_flag: bool
    default_model: Optional[str] = None


def build_agy_argv(
    prompt: str,
    model: Optional[str] = None,
    effort: Optional[str] = None,
    workspace_path: Optional[str] = None,
) -> List[str]:
    spec = AGENTS["agy"]["spec"]
    cmd = ["agy", "-p", prompt]


    settings = get_settings()
    selected_model = model or settings.agy_default_model or spec.default_model

    if selected_model:
        if not spec.supports_model_flag:
            raise ValueError("Agent 'agy' does not support model overrides")
        cmd.extend(["--model", selected_model])

    if effort:
        cmd.extend(["--effort", effort])

    if settings.sandbox_enabled and settings.agy_sandbox_flags:
        for flag in settings.agy_sandbox_flags.split():
            if flag:
                cmd.append(flag)


    if workspace_path:
        cmd.extend(["--add-dir", workspace_path])

    cmd.extend(["--output-format", "text"])
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
    selected_model = model or settings.claude_default_model or spec.default_model

    if selected_model:
        if not spec.supports_model_flag:
            raise ValueError("Agent 'claude' does not support model overrides")
        cmd.extend(["--model", selected_model])

    if effort:
        cmd.extend(["--effort", effort])

    if settings.sandbox_enabled:
        cmd.extend(["--allowed-tools", "View,Read"])
        cmd.extend(["--permission-mode", "dontAsk"])

    cmd.extend(["--output-format", "text"])
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

