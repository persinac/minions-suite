"""Every tool a role's schema offers must be routable by McpToolExecutor.

The gap this guards against is quiet and cruel: a tool that is in the schema
but not in the executor's routing tables is offered to the model, called by
the model, and answered with {"error": "Unknown tool"}. The model retries,
paraphrases, gives up. `report_no_work_needed` sat in exactly that state —
declared for engineers (definitions.py), implemented on the MCP server
(server/mcp.py), and routable by nothing — which made TaskStatus.NO_WORK_NEEDED
unreachable from any in-process engineer. The failure class it feeds once
wedged all intake for two days (job 7b840e7f's family).

The existing coherence test compares prompts to schemas
(test_prompt_tool_coherence.py); nothing compared schemas to the executor.
This does. Scoped to the roles create_mcp_tool_executor actually serves —
code_reviewer is excluded because it deliberately gets ToolExecutor via the
provider path instead.
"""

import pytest

from minions.agents.tools.definitions import get_tools_for_role
from minions.agents.tools.mcp_executor import _LOCAL_TOOLS, _STATE_TOOL_INJECTIONS

# Every role create_mcp_tool_executor builds a McpToolExecutor for.
MCP_EXECUTOR_ROLES = [
    "spec_analyst",
    "arbiter",
    "backend_engineer",
    "frontend_engineer",
    "database_engineer",
    "deploy_monitor",
    "finisher",
]

ROUTABLE = set(_STATE_TOOL_INJECTIONS) | set(_LOCAL_TOOLS)


def _offered(role: str, memory_enabled: bool) -> set[str]:
    return {t["function"]["name"] for t in get_tools_for_role(role, memory_enabled=memory_enabled)}


@pytest.mark.parametrize("role", MCP_EXECUTOR_ROLES)
@pytest.mark.parametrize("memory_enabled", [False, True], ids=["memory-off", "memory-on"])
def test_every_offered_tool_is_routable(role, memory_enabled):
    unroutable = _offered(role, memory_enabled) - ROUTABLE
    assert not unroutable, f"{role} is offered tools the executor answers with 'Unknown tool': {sorted(unroutable)}"


def test_report_no_work_needed_reaches_the_engineer():
    """The specific instance that motivated this file, pinned by name."""
    assert "report_no_work_needed" in _offered("backend_engineer", memory_enabled=False)
    assert "report_no_work_needed" in _STATE_TOOL_INJECTIONS
    # task_id is injected from context, mirroring report_pr — the model only
    # supplies `reason`.
    assert ("task_id", "task_id") in _STATE_TOOL_INJECTIONS["report_no_work_needed"]
