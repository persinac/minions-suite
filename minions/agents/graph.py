"""LangGraph subgraph wrapper for the LiteLLM agent tool-use loop.

Wraps `_agent_loop_generic()` as a single LangGraph node so the orchestration
layer can treat agent execution as a graph step with checkpointing and retry,
while preserving the existing wind-down mechanism and cost tracking.
"""

import logging
from typing import Any, TypedDict

import litellm
from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy

from .runner import _agent_loop_generic

logger = logging.getLogger(__name__)


class AgentSubgraphState(TypedDict):
    """State schema for the agent execution subgraph."""

    # Inputs (set by parent graph / caller)
    job_id: str
    task_id: str
    agent_id: str
    agent_role: str
    model: str
    system_prompt: str
    tools: list[dict]
    tool_executor: Any  # ToolExecutor or McpToolExecutor
    timeout: int
    max_turns: int
    log_path: str
    user_message: str | None
    db: Any  # AbstractDatabase or None

    # Outputs (set by execution node)
    result: dict | None
    error: str | None


async def agent_execution_node(state: AgentSubgraphState) -> dict:
    """Execute the LiteLLM tool-use loop and return results.

    This wraps `_agent_loop_generic()` as a single graph node. The existing
    wind-down mechanism, cost tracking, and tool call recording are preserved
    inside the loop — LangGraph only sees the final result.
    """
    from pathlib import Path

    try:
        result = await _agent_loop_generic(
            model=state["model"],
            system_prompt=state["system_prompt"],
            tools=state["tools"],
            tool_executor=state["tool_executor"],
            timeout=state["timeout"],
            log_path=Path(state["log_path"]),
            user_message=state.get("user_message"),
            max_turns=state["max_turns"],
            db=state.get("db"),
            job_id=state["job_id"],
        )
        return {"result": result, "error": None}
    except Exception as e:
        logger.error("Agent execution node failed: %s", e, exc_info=True)
        return {"result": None, "error": str(e)[:500]}


def build_agent_subgraph() -> StateGraph:
    """Build and return a compiled agent execution subgraph.

    Single node graph: START → execute → END
    with retry policy for rate limit errors.
    """
    graph = StateGraph(AgentSubgraphState)

    retry_policy = RetryPolicy(
        max_attempts=5,
        initial_interval=5.0,
        backoff_factor=2.0,
        max_interval=60.0,
        retry_on=_is_retryable_error,
    )

    graph.add_node("execute", agent_execution_node, retry_policy=retry_policy)
    graph.add_edge(START, "execute")
    graph.add_edge("execute", END)

    return graph.compile()


def _is_retryable_error(exc: BaseException) -> bool:
    """Check if an exception is a retryable LLM API error (rate limit or transient server error)."""
    return isinstance(exc, (litellm.exceptions.RateLimitError, litellm.exceptions.InternalServerError))
