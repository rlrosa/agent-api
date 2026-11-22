from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field


class AgentAvailability(BaseModel):
    available: bool
    path: Optional[str] = None


class HealthResponse(BaseModel):
    version: str
    max_concurrency: int
    queue_depth: int
    running_count: int
    effective_concurrency: int
    agents: Dict[str, AgentAvailability]
    confinement: Optional[Dict[str, Any]] = None
    active_sessions: int = 0
    max_active_sessions: int = 10



class AttachmentSpec(BaseModel):
    url: Optional[str] = None
    filename: Optional[str] = None
    content_b64: Optional[str] = None

    model_config = ConfigDict(extra="forbid")


class CreateJobRequest(BaseModel):
    agent: Optional[str] = None
    prompt: str
    model: Optional[str] = None
    effort: Optional[str] = None
    attachments: Optional[List[AttachmentSpec]] = None
    wait: int = 60
    timeout: int = 120
    metadata: Optional[Dict[str, Any]] = None

    model_config = ConfigDict(extra="forbid")


class CreateSessionRequest(BaseModel):
    agent: Optional[str] = None
    prompt: Optional[str] = None
    model: Optional[str] = None
    effort: Optional[str] = None
    attachments: Optional[List[AttachmentSpec]] = None
    idle_timeout: Optional[int] = None
    turn_timeout: Optional[int] = None
    wait: int = 60
    metadata: Optional[Dict[str, Any]] = None

    model_config = ConfigDict(extra="forbid")


class SessionMessageRequest(BaseModel):
    prompt: str
    attachments: Optional[List[AttachmentSpec]] = None
    turn_timeout: Optional[int] = None
    wait: int = 60

    model_config = ConfigDict(extra="forbid")

