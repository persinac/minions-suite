"""Tests for tool definitions and get_tools_for_role."""

from minions.agents.tools.definitions import (
    ARBITER_TOOL_DEFINITIONS,
    DEPLOY_TOOL_DEFINITIONS,
    ENGINEER_TOOL_DEFINITIONS,
    REVIEW_TOOL_DEFINITIONS,
    SPEC_ANALYST_TOOL_DEFINITIONS,
    get_tools_for_role,
)


def _tool_names(tools: list) -> set[str]:
    """Extract tool names from a list of tool definitions."""
    return {t["function"]["name"] for t in tools}


class TestToolDefinitionStructure:
    def test_review_tools_valid_schema(self):
        for tool in REVIEW_TOOL_DEFINITIONS:
            assert tool["type"] == "function"
            assert "name" in tool["function"]
            assert "description" in tool["function"]
            assert "parameters" in tool["function"]
            params = tool["function"]["parameters"]
            assert params["type"] == "object"

    def test_spec_analyst_tools_valid_schema(self):
        for tool in SPEC_ANALYST_TOOL_DEFINITIONS:
            assert tool["type"] == "function"
            assert "name" in tool["function"]

    def test_arbiter_tools_valid_schema(self):
        for tool in ARBITER_TOOL_DEFINITIONS:
            assert tool["type"] == "function"
            assert "name" in tool["function"]

    def test_engineer_tools_valid_schema(self):
        for tool in ENGINEER_TOOL_DEFINITIONS:
            assert tool["type"] == "function"
            assert "name" in tool["function"]

    def test_deploy_tools_valid_schema(self):
        for tool in DEPLOY_TOOL_DEFINITIONS:
            assert tool["type"] == "function"
            assert "name" in tool["function"]


class TestReviewTools:
    def test_expected_tools(self):
        names = _tool_names(REVIEW_TOOL_DEFINITIONS)
        expected = {"get_mr_diff", "get_changed_files", "read_file", "search_code",
                    "list_files", "get_mr_comments", "post_inline_comment", "submit_review"}
        assert names == expected

    def test_count(self):
        assert len(REVIEW_TOOL_DEFINITIONS) == 8


class TestSpecAnalystTools:
    def test_expected_tools(self):
        names = _tool_names(SPEC_ANALYST_TOOL_DEFINITIONS)
        assert "submit_refined_spec" in names
        assert "send_message" in names
        assert "send_heartbeat" in names

    def test_no_arbiter_tools(self):
        names = _tool_names(SPEC_ANALYST_TOOL_DEFINITIONS)
        assert "create_task" not in names
        assert "mark_tasks_created" not in names

    def test_count(self):
        assert len(SPEC_ANALYST_TOOL_DEFINITIONS) == 3


class TestArbiterTools:
    def test_expected_tools(self):
        names = _tool_names(ARBITER_TOOL_DEFINITIONS)
        assert "submit_refined_spec" in names
        assert "create_task" in names
        assert "mark_tasks_created" in names
        assert "send_message" in names
        assert "send_heartbeat" in names

    def test_count(self):
        assert len(ARBITER_TOOL_DEFINITIONS) == 5


class TestEngineerTools:
    def test_has_filesystem_tools(self):
        names = _tool_names(ENGINEER_TOOL_DEFINITIONS)
        assert "read_file" in names
        assert "write_file" in names
        assert "run_command" in names
        assert "search_code" in names

    def test_has_git_tools(self):
        names = _tool_names(ENGINEER_TOOL_DEFINITIONS)
        assert "create_branch" in names
        assert "commit" in names
        assert "push" in names
        assert "create_pr" in names
        assert "report_pr" in names

    def test_has_subtask_tools(self):
        names = _tool_names(ENGINEER_TOOL_DEFINITIONS)
        assert "submit_subtask_plan" in names
        assert "start_subtask" in names
        assert "complete_subtask" in names
        assert "fail_subtask" in names
        assert "get_subtasks" in names

    def test_has_status_and_heartbeat(self):
        names = _tool_names(ENGINEER_TOOL_DEFINITIONS)
        assert "update_task_status" in names
        assert "send_heartbeat" in names

    def test_count(self):
        assert len(ENGINEER_TOOL_DEFINITIONS) == 16


class TestDeployTools:
    def test_expected_tools(self):
        names = _tool_names(DEPLOY_TOOL_DEFINITIONS)
        expected = {
            "check_ci_status", "report_deploy_status", "update_task_status", "send_heartbeat",
            "submit_subtask_plan", "start_subtask", "complete_subtask", "fail_subtask", "get_subtasks",
            "send_message",
        }
        assert names == expected

    def test_count(self):
        assert len(DEPLOY_TOOL_DEFINITIONS) == 10


class TestGetToolsForRole:
    def test_spec_analyst(self):
        tools = get_tools_for_role("spec_analyst")
        assert tools is SPEC_ANALYST_TOOL_DEFINITIONS

    def test_arbiter(self):
        tools = get_tools_for_role("arbiter")
        assert tools is ARBITER_TOOL_DEFINITIONS

    def test_spec_analyst_and_arbiter_are_different(self):
        spec_tools = get_tools_for_role("spec_analyst")
        arbiter_tools = get_tools_for_role("arbiter")
        assert spec_tools is not arbiter_tools
        spec_names = _tool_names(spec_tools)
        arbiter_names = _tool_names(arbiter_tools)
        assert "create_task" not in spec_names
        assert "create_task" in arbiter_names

    def test_backend_engineer(self):
        tools = get_tools_for_role("backend_engineer")
        assert tools is ENGINEER_TOOL_DEFINITIONS

    def test_frontend_engineer(self):
        tools = get_tools_for_role("frontend_engineer")
        assert tools is ENGINEER_TOOL_DEFINITIONS

    def test_database_engineer(self):
        from minions.agents.tools.definitions import DB_ENGINEER_TOOL_DEFINITIONS
        tools = get_tools_for_role("database_engineer")
        assert tools is DB_ENGINEER_TOOL_DEFINITIONS

    def test_code_reviewer(self):
        tools = get_tools_for_role("code_reviewer")
        from minions.agents.tools.definitions import CODE_REVIEWER_TOOL_DEFINITIONS
        assert tools is CODE_REVIEWER_TOOL_DEFINITIONS

    def test_deploy_monitor(self):
        tools = get_tools_for_role("deploy_monitor")
        assert tools is DEPLOY_TOOL_DEFINITIONS

    def test_unknown_role_gets_engineer_tools(self):
        tools = get_tools_for_role("unknown_role")
        assert tools is ENGINEER_TOOL_DEFINITIONS

    def test_no_tool_name_collisions_within_role(self):
        """Each role's tool list should have unique tool names."""
        for role in ["spec_analyst", "arbiter", "backend_engineer", "database_engineer", "code_reviewer", "deploy_monitor"]:
            tools = get_tools_for_role(role)
            names = [t["function"]["name"] for t in tools]
            assert len(names) == len(set(names)), f"Duplicate tool names for role {role}"
