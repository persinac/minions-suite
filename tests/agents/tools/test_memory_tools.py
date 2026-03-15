"""Tests for memory tool definitions gated by memory_enabled flag."""

from minions.agents.tools.definitions import _MEMORY_TOOLS, get_tools_for_role


def _tool_names(tools: list[dict]) -> set[str]:
    return {t["function"]["name"] for t in tools}


MEMORY_TOOL_NAMES = {"publish_fact", "query_facts", "create_memory_note"}


def test_memory_tools_not_present_by_default():
    """When memory_enabled is False (default), no memory tools should appear."""
    for role in ("code_reviewer", "backend_engineer", "frontend_engineer", "database_engineer", "spec_analyst", "arbiter", "deploy_monitor"):
        tools = get_tools_for_role(role)
        names = _tool_names(tools)
        assert not names & MEMORY_TOOL_NAMES, f"Memory tools found for {role} without memory_enabled"


def test_memory_tools_present_when_enabled():
    """When memory_enabled is True, memory tools should appear for all roles."""
    for role in ("code_reviewer", "backend_engineer", "frontend_engineer", "database_engineer", "spec_analyst", "arbiter", "deploy_monitor"):
        tools = get_tools_for_role(role, memory_enabled=True)
        names = _tool_names(tools)
        assert names >= MEMORY_TOOL_NAMES, f"Missing memory tools for {role} with memory_enabled=True"


def test_memory_tools_schemas_valid():
    """All memory tool schemas should have required fields."""
    for tool in _MEMORY_TOOLS:
        assert tool["type"] == "function"
        fn = tool["function"]
        assert "name" in fn
        assert "description" in fn
        assert "parameters" in fn
        assert fn["parameters"]["type"] == "object"


def test_existing_tools_unchanged_when_memory_enabled():
    """Enabling memory should add tools, not remove existing ones."""
    for role in ("code_reviewer", "backend_engineer"):
        base = _tool_names(get_tools_for_role(role, memory_enabled=False))
        with_memory = _tool_names(get_tools_for_role(role, memory_enabled=True))
        assert base <= with_memory, f"Existing tools missing for {role} when memory_enabled=True"
