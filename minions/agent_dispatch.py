"""Data models for NATS work queue messages.

Defines the contracts for dispatching agent work items to remote workers
and receiving results back. Used by the job engine (publish) and worker
containers (consume).
"""

import json
from dataclasses import asdict, dataclass


@dataclass
class AgentWorkItem:
    """Everything a worker needs to run a LiteLLM agent loop."""

    job_id: str
    agent_id: str
    role: str  # AgentRole value (e.g. "backend_engineer")
    prompt: str  # Full assembled prompt text
    working_dir: str  # e.g. /repos/management-api
    allowed_tools: list[str]
    mcp_url: str  # e.g. http://mcp-server:8321/sse
    timeout: int  # seconds
    model: str  # LiteLLM model string
    dry_run: bool
    env_overrides: dict[str, str]  # GIT_AUTHOR_NAME, etc.
    repo_clone_url: str = ""  # git clone URL for K8s init container


@dataclass
class AgentResultMessage:
    """What the worker reports back after finishing an agent run."""

    job_id: str
    agent_id: str
    role: str
    success: bool
    return_code: int
    log_file: str
    stderr_tail: str  # first 500 chars
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    cost_usd: float
    num_turns: int
    model: str


def serialize_work_item(item: AgentWorkItem) -> bytes:
    """JSON-encode an AgentWorkItem to bytes for NATS publishing."""
    return json.dumps(asdict(item)).encode("utf-8")


def deserialize_work_item(data: bytes) -> AgentWorkItem:
    """Decode bytes from NATS into an AgentWorkItem."""
    d = json.loads(data.decode("utf-8"))
    return AgentWorkItem(**d)


def serialize_result(msg: AgentResultMessage) -> bytes:
    """JSON-encode an AgentResultMessage to bytes for NATS publishing."""
    return json.dumps(asdict(msg)).encode("utf-8")


def deserialize_result(data: bytes) -> AgentResultMessage:
    """Decode bytes from NATS into an AgentResultMessage."""
    d = json.loads(data.decode("utf-8"))
    return AgentResultMessage(**d)
