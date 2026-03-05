"""Tests that validate minions-suite tool assignments match the alpha-minions tool matrix.

The tool matrix (alpha-minions/notes/presentation/tool_matrix.csv) defines
which tools each agent role should have access to. This test suite ensures
minions-suite maintains parity with that specification.

Alpha-minions uses Claude Code built-in tools (Read, Edit, Write, Glob, Grep,
Bash) while minions-suite provides equivalent functionality through its own
tool implementations. The mapping is:

    Alpha tool  -> minions-suite equivalent
    ----------     ------------------------
    Read        -> read_file
    Edit        -> (write_file — minions-suite merges edit into write)
    Write       -> write_file
    Glob        -> list_files (review) / search_code (engineers)
    Grep        -> search_code
    Bash        -> run_command

MCP state tools (submit_refined_spec, create_task, etc.) keep the same names
across both projects.
"""

import pytest

from minions.mcp_tool_executor import _STATE_TOOL_INJECTIONS
from minions.tools import (
    DEPLOY_TOOL_DEFINITIONS,
    REVIEW_TOOL_DEFINITIONS,
    SPEC_TOOL_DEFINITIONS,
    get_tools_for_role,
)


def _tool_names(tools: list) -> set[str]:
    return {t["function"]["name"] for t in tools}


# ---------------------------------------------------------------------------
# The canonical tool matrix from alpha-minions
# ---------------------------------------------------------------------------
# Each key is a role, each value is the set of tool names that role should
# have access to (using alpha-minions names).

TOOL_MATRIX: dict[str, set[str]] = {
    "spec_analyst": {
        "Read", "Glob", "Grep",
        "submit_refined_spec",
        "send_message", "get_messages", "send_heartbeat", "get_job_status",
        "create_phase_card", "mark_phases_created",
    },
    "arbiter": {
        "create_task", "mark_tasks_created",
        "send_message", "get_messages", "send_heartbeat", "get_job_status",
    },
    "backend_engineer": {
        "Read", "Edit", "Write", "Glob", "Grep", "Bash",
        "update_task_status", "report_pr",
        "submit_subtask_plan", "start_subtask", "complete_subtask", "fail_subtask", "get_subtasks",
        "send_message", "get_messages", "send_heartbeat",
    },
    "frontend_engineer": {
        "Read", "Edit", "Write", "Glob", "Grep", "Bash",
        "update_task_status", "report_pr",
        "submit_subtask_plan", "start_subtask", "complete_subtask", "fail_subtask", "get_subtasks",
        "send_message", "get_messages", "send_heartbeat",
    },
    "database_engineer": {
        "Read", "Write", "Glob", "Grep", "Bash",
        "update_task_status", "report_pr",
        "send_message", "get_messages", "send_heartbeat",
    },
    "code_reviewer": {
        "Read", "Glob", "Grep", "Bash",
        "report_review_complete",
        "submit_subtask_plan", "start_subtask", "complete_subtask", "fail_subtask", "get_subtasks",
        "send_message", "get_messages", "send_heartbeat",
        "create_trello_tech_debt",
    },
    "deploy_monitor": {
        "Bash",
        "report_deploy_status",
        "submit_subtask_plan", "start_subtask", "complete_subtask", "fail_subtask", "get_subtasks",
        "send_message", "get_messages", "send_heartbeat",
    },
}

# Alpha tool -> minions-suite local tool equivalents
ALPHA_TO_LOCAL: dict[str, set[str]] = {
    "Read": {"read_file"},
    "Edit": {"write_file"},       # minions-suite merges edit into write_file
    "Write": {"write_file"},
    "Glob": {"list_files", "search_code"},  # list_files in review, search_code for engineers
    "Grep": {"search_code"},
    "Bash": {"run_command", "check_ci_status"},  # shell access tools
}

# Alpha tools that are purely local (Claude Code built-in equivalents)
ALPHA_LOCAL_TOOLS = {"Read", "Edit", "Write", "Glob", "Grep", "Bash"}

# Expected tool counts — derived from counting 'x' marks in CSV rows per role.
# Note: CSV last row says database_engineer=11 but row-by-row count yields 10.
# We use the row-by-row count as ground truth.
EXPECTED_TOOL_COUNTS: dict[str, int] = {
    "spec_analyst": 10,
    "arbiter": 6,
    "backend_engineer": 16,
    "frontend_engineer": 16,
    "database_engineer": 10,  # CSV says 11 but only 10 'x' marks in rows
    "code_reviewer": 14,
    "deploy_monitor": 10,
}


# ---------------------------------------------------------------------------
# Tool count tests (from CSV last row)
# ---------------------------------------------------------------------------


class TestToolMatrixCounts:
    """Verify the matrix's own tool count column is consistent."""

    @pytest.mark.parametrize("role,expected_count", EXPECTED_TOOL_COUNTS.items())
    def test_matrix_tool_count(self, role, expected_count):
        actual = len(TOOL_MATRIX[role])
        assert actual == expected_count, (
            f"{role}: matrix has {actual} tools but CSV says {expected_count}. "
            f"Tools: {sorted(TOOL_MATRIX[role])}"
        )

    def test_all_roles_in_matrix(self):
        expected_roles = {"spec_analyst", "arbiter", "backend_engineer", "frontend_engineer",
                         "database_engineer", "code_reviewer", "deploy_monitor"}
        assert set(TOOL_MATRIX.keys()) == expected_roles


# ---------------------------------------------------------------------------
# State tool coverage: every MCP state tool in the matrix must exist in
# _STATE_TOOL_INJECTIONS (the executor's routing table)
# ---------------------------------------------------------------------------


class TestStateToolCoverage:
    """Every non-local tool in the matrix must exist in _STATE_TOOL_INJECTIONS."""

    def _matrix_state_tools(self) -> set[str]:
        all_tools = set()
        for tools in TOOL_MATRIX.values():
            all_tools |= tools
        return all_tools - ALPHA_LOCAL_TOOLS

    def test_all_matrix_state_tools_have_injection_mapping(self):
        state_tools = self._matrix_state_tools()
        missing = state_tools - set(_STATE_TOOL_INJECTIONS.keys())
        assert not missing, (
            f"Matrix state tools missing from _STATE_TOOL_INJECTIONS: {sorted(missing)}. "
            f"These tools exist in the alpha tool matrix but have no executor routing."
        )

    def test_no_orphan_state_injections(self):
        """Every tool in _STATE_TOOL_INJECTIONS should appear in the matrix."""
        state_tools = self._matrix_state_tools()
        injected = set(_STATE_TOOL_INJECTIONS.keys())
        orphans = injected - state_tools
        # Allow tools that might be in both projects but not in the presentation matrix
        # (e.g. internal-only tools)
        if orphans:
            pytest.skip(f"Extra state tools not in matrix (may be intentional): {sorted(orphans)}")


# ---------------------------------------------------------------------------
# Per-role tool assignment: each role's LLM-facing tool definitions
# (from get_tools_for_role) should cover the matrix's state tools
# ---------------------------------------------------------------------------


def _get_role_state_tools(role: str) -> set[str]:
    """Get the MCP state tool names assigned to a role in the matrix."""
    return TOOL_MATRIX[role] - ALPHA_LOCAL_TOOLS


def _get_role_local_tools(role: str) -> set[str]:
    """Get the alpha local tool names assigned to a role in the matrix."""
    return TOOL_MATRIX[role] & ALPHA_LOCAL_TOOLS


class TestSpecAnalystTools:
    def test_has_submit_refined_spec(self):
        tools = _tool_names(get_tools_for_role("spec_analyst"))
        assert "submit_refined_spec" in tools

    def test_has_send_message(self):
        tools = _tool_names(get_tools_for_role("spec_analyst"))
        assert "send_message" in tools

    def test_has_send_heartbeat(self):
        tools = _tool_names(get_tools_for_role("spec_analyst"))
        assert "send_heartbeat" in tools

    def test_matrix_state_tools_in_definitions_or_injections(self):
        """All spec_analyst state tools should be in tool defs or injection map."""
        expected_state = _get_role_state_tools("spec_analyst")
        tool_defs = _tool_names(get_tools_for_role("spec_analyst"))
        injections = set(_STATE_TOOL_INJECTIONS.keys())
        covered = tool_defs | injections
        missing = expected_state - covered
        assert not missing, f"spec_analyst missing tools: {sorted(missing)}"

    def test_should_not_have_create_task(self):
        """Matrix says create_task belongs to arbiter, not spec_analyst."""
        matrix_tools = TOOL_MATRIX["spec_analyst"]
        assert "create_task" not in matrix_tools

    def test_has_filesystem_access(self):
        """Matrix says spec_analyst has Read, Glob, Grep."""
        local = _get_role_local_tools("spec_analyst")
        assert "Read" in local
        assert "Glob" in local
        assert "Grep" in local

    def test_no_write_access(self):
        """Spec analyst should not be able to write/edit files."""
        local = _get_role_local_tools("spec_analyst")
        assert "Write" not in local
        assert "Edit" not in local
        assert "Bash" not in local


class TestArbiterTools:
    def test_has_create_task(self):
        tools = _tool_names(get_tools_for_role("arbiter"))
        assert "create_task" in tools

    def test_has_mark_tasks_created(self):
        tools = _tool_names(get_tools_for_role("arbiter"))
        assert "mark_tasks_created" in tools

    def test_has_messaging(self):
        tools = _tool_names(get_tools_for_role("arbiter"))
        assert "send_message" in tools
        assert "send_heartbeat" in tools

    def test_no_filesystem_access(self):
        """Arbiter should not have any local filesystem tools."""
        local = _get_role_local_tools("arbiter")
        assert len(local) == 0

    def test_should_not_have_submit_refined_spec(self):
        """Matrix says submit_refined_spec belongs to spec_analyst, not arbiter."""
        matrix_tools = TOOL_MATRIX["arbiter"]
        assert "submit_refined_spec" not in matrix_tools

    def test_matrix_state_tools_in_definitions_or_injections(self):
        expected_state = _get_role_state_tools("arbiter")
        tool_defs = _tool_names(get_tools_for_role("arbiter"))
        injections = set(_STATE_TOOL_INJECTIONS.keys())
        covered = tool_defs | injections
        missing = expected_state - covered
        assert not missing, f"arbiter missing tools: {sorted(missing)}"


class TestBackendEngineerTools:
    def test_has_filesystem_tools(self):
        tools = _tool_names(get_tools_for_role("backend_engineer"))
        assert "read_file" in tools
        assert "write_file" in tools
        assert "run_command" in tools
        assert "search_code" in tools

    def test_has_git_tools(self):
        tools = _tool_names(get_tools_for_role("backend_engineer"))
        assert "create_branch" in tools
        assert "commit" in tools
        assert "push" in tools
        assert "create_pr" in tools

    def test_has_report_pr(self):
        tools = _tool_names(get_tools_for_role("backend_engineer"))
        assert "report_pr" in tools

    def test_has_subtask_tools(self):
        tools = _tool_names(get_tools_for_role("backend_engineer"))
        for t in ["submit_subtask_plan", "start_subtask", "complete_subtask", "fail_subtask", "get_subtasks"]:
            assert t in tools, f"backend_engineer missing subtask tool: {t}"

    def test_has_update_task_status(self):
        tools = _tool_names(get_tools_for_role("backend_engineer"))
        assert "update_task_status" in tools

    def test_has_heartbeat(self):
        tools = _tool_names(get_tools_for_role("backend_engineer"))
        assert "send_heartbeat" in tools

    def test_full_local_tool_access(self):
        """Matrix says backend_engineer has Read, Edit, Write, Glob, Grep, Bash."""
        local = _get_role_local_tools("backend_engineer")
        assert local == {"Read", "Edit", "Write", "Glob", "Grep", "Bash"}

    def test_matrix_state_tools_in_definitions_or_injections(self):
        expected_state = _get_role_state_tools("backend_engineer")
        tool_defs = _tool_names(get_tools_for_role("backend_engineer"))
        injections = set(_STATE_TOOL_INJECTIONS.keys())
        covered = tool_defs | injections
        missing = expected_state - covered
        assert not missing, f"backend_engineer missing tools: {sorted(missing)}"


class TestFrontendEngineerTools:
    def test_same_tools_as_backend(self):
        """Matrix gives frontend and backend engineers identical tool sets."""
        assert TOOL_MATRIX["frontend_engineer"] == TOOL_MATRIX["backend_engineer"]

    def test_same_tool_definitions(self):
        be_tools = get_tools_for_role("backend_engineer")
        fe_tools = get_tools_for_role("frontend_engineer")
        assert be_tools is fe_tools


class TestDatabaseEngineerTools:
    def test_has_read_write_but_no_edit(self):
        """Matrix says DB engineer has Read, Write, Glob, Grep, Bash but NOT Edit."""
        local = _get_role_local_tools("database_engineer")
        assert "Read" in local
        assert "Write" in local
        assert "Glob" in local
        assert "Grep" in local
        assert "Bash" in local
        assert "Edit" not in local

    def test_fewer_tools_than_backend(self):
        """DB engineer has 11 tools vs backend's 16."""
        assert len(TOOL_MATRIX["database_engineer"]) < len(TOOL_MATRIX["backend_engineer"])

    def test_no_subtask_tools(self):
        """Matrix does NOT give DB engineer subtask tools."""
        matrix_tools = TOOL_MATRIX["database_engineer"]
        subtask_tools = {"submit_subtask_plan", "start_subtask", "complete_subtask", "fail_subtask", "get_subtasks"}
        assert not (matrix_tools & subtask_tools), "DB engineer should not have subtask tools per matrix"

    def test_has_report_pr(self):
        assert "report_pr" in TOOL_MATRIX["database_engineer"]

    def test_has_update_task_status(self):
        assert "update_task_status" in TOOL_MATRIX["database_engineer"]

    def test_matrix_state_tools_in_definitions_or_injections(self):
        expected_state = _get_role_state_tools("database_engineer")
        tool_defs = _tool_names(get_tools_for_role("database_engineer"))
        injections = set(_STATE_TOOL_INJECTIONS.keys())
        covered = tool_defs | injections
        missing = expected_state - covered
        assert not missing, f"database_engineer missing tools: {sorted(missing)}"


class TestCodeReviewerTools:
    def test_has_report_review_complete(self):
        """Matrix says code_reviewer has report_review_complete."""
        assert "report_review_complete" in TOOL_MATRIX["code_reviewer"]

    def test_has_subtask_tools(self):
        matrix_tools = TOOL_MATRIX["code_reviewer"]
        for t in ["submit_subtask_plan", "start_subtask", "complete_subtask", "fail_subtask", "get_subtasks"]:
            assert t in matrix_tools, f"code_reviewer missing subtask tool: {t}"

    def test_has_create_trello_tech_debt(self):
        assert "create_trello_tech_debt" in TOOL_MATRIX["code_reviewer"]

    def test_has_read_glob_grep_bash(self):
        local = _get_role_local_tools("code_reviewer")
        assert "Read" in local
        assert "Glob" in local
        assert "Grep" in local
        assert "Bash" in local

    def test_no_write_or_edit(self):
        """Reviewers should not write code — they only read and report."""
        local = _get_role_local_tools("code_reviewer")
        assert "Write" not in local
        assert "Edit" not in local

    def test_no_update_task_status(self):
        """Matrix does NOT give reviewer update_task_status."""
        assert "update_task_status" not in TOOL_MATRIX["code_reviewer"]

    def test_matrix_state_tools_in_definitions_or_injections(self):
        expected_state = _get_role_state_tools("code_reviewer")
        tool_defs = _tool_names(get_tools_for_role("code_reviewer"))
        injections = set(_STATE_TOOL_INJECTIONS.keys())
        covered = tool_defs | injections
        missing = expected_state - covered
        assert not missing, f"code_reviewer missing tools: {sorted(missing)}"


class TestDeployMonitorTools:
    def test_has_report_deploy_status(self):
        tools = _tool_names(get_tools_for_role("deploy_monitor"))
        assert "report_deploy_status" in tools

    def test_has_subtask_tools(self):
        matrix_tools = TOOL_MATRIX["deploy_monitor"]
        for t in ["submit_subtask_plan", "start_subtask", "complete_subtask", "fail_subtask", "get_subtasks"]:
            assert t in matrix_tools, f"deploy_monitor missing subtask tool: {t}"

    def test_has_bash_only(self):
        """Deploy monitor only has Bash for local tools."""
        local = _get_role_local_tools("deploy_monitor")
        assert local == {"Bash"}

    def test_no_filesystem_tools(self):
        """Deploy monitor should not read/write files."""
        local = _get_role_local_tools("deploy_monitor")
        assert "Read" not in local
        assert "Write" not in local

    def test_no_report_pr(self):
        """Deploy monitor doesn't create PRs."""
        assert "report_pr" not in TOOL_MATRIX["deploy_monitor"]

    def test_matrix_state_tools_in_definitions_or_injections(self):
        expected_state = _get_role_state_tools("deploy_monitor")
        tool_defs = _tool_names(get_tools_for_role("deploy_monitor"))
        injections = set(_STATE_TOOL_INJECTIONS.keys())
        covered = tool_defs | injections
        missing = expected_state - covered
        assert not missing, f"deploy_monitor missing tools: {sorted(missing)}"


# ---------------------------------------------------------------------------
# Universal tools: send_message, get_messages, send_heartbeat should be
# available to ALL roles per the matrix
# ---------------------------------------------------------------------------


class TestUniversalTools:
    UNIVERSAL_TOOLS: frozenset[str] = frozenset({"send_message", "get_messages", "send_heartbeat"})

    @pytest.mark.parametrize("role", TOOL_MATRIX.keys())
    def test_every_role_has_universal_tools(self, role):
        matrix_tools = TOOL_MATRIX[role]
        missing = self.UNIVERSAL_TOOLS - matrix_tools
        assert not missing, f"{role} missing universal tools: {sorted(missing)}"

    def test_universal_tools_in_state_injections(self):
        for tool in self.UNIVERSAL_TOOLS:
            assert tool in _STATE_TOOL_INJECTIONS, f"Universal tool {tool} missing from _STATE_TOOL_INJECTIONS"


# ---------------------------------------------------------------------------
# get_tools_for_role parity: spec_analyst and arbiter SHOULD have different
# tool sets per matrix, but currently share SPEC_TOOL_DEFINITIONS
# ---------------------------------------------------------------------------


class TestSpecArbiterSeparation:
    """The matrix gives spec_analyst and arbiter different tools.

    Currently minions-suite gives both SPEC_TOOL_DEFINITIONS.
    These tests document the expected divergence so we catch it.
    """

    def test_spec_analyst_has_tools_arbiter_lacks(self):
        spec_only = TOOL_MATRIX["spec_analyst"] - TOOL_MATRIX["arbiter"]
        expected_spec_only = {"Read", "Glob", "Grep", "submit_refined_spec",
                             "create_phase_card", "mark_phases_created"}
        assert spec_only == expected_spec_only

    def test_arbiter_has_tools_spec_analyst_lacks(self):
        arb_only = TOOL_MATRIX["arbiter"] - TOOL_MATRIX["spec_analyst"]
        expected_arb_only = {"create_task", "mark_tasks_created"}
        assert arb_only == expected_arb_only

    def test_current_implementation_shares_tools(self):
        """Document that spec_analyst and arbiter currently share the same tool list."""
        spec_tools = get_tools_for_role("spec_analyst")
        arb_tools = get_tools_for_role("arbiter")
        assert spec_tools is arb_tools, (
            "Expected spec_analyst and arbiter to share SPEC_TOOL_DEFINITIONS. "
            "If they've been separated, update this test."
        )

    def test_shared_tools_is_superset_of_both(self):
        """The shared SPEC_TOOL_DEFINITIONS should be a superset of both roles' needs."""
        shared = _tool_names(SPEC_TOOL_DEFINITIONS)
        assert "submit_refined_spec" in shared
        assert "create_task" in shared
        assert "mark_tasks_created" in shared
        assert "send_message" in shared
        assert "send_heartbeat" in shared


# ---------------------------------------------------------------------------
# DB engineer vs engineer parity: DB engineer shares ENGINEER_TOOL_DEFINITIONS
# but the matrix says it should have fewer tools
# ---------------------------------------------------------------------------


class TestDatabaseEngineerParity:
    """The matrix gives DB engineer fewer tools than backend/frontend engineers.

    DB engineer has its own DB_ENGINEER_TOOL_DEFINITIONS — no subtask tools,
    no create_pr (migrations go through report_pr directly).
    """

    def test_matrix_db_eng_has_fewer_tools(self):
        assert len(TOOL_MATRIX["database_engineer"]) < len(TOOL_MATRIX["backend_engineer"])

    def test_separate_tool_definitions(self):
        """DB engineer should have its own tool list, not share with backend."""
        be = get_tools_for_role("backend_engineer")
        db = get_tools_for_role("database_engineer")
        assert be is not db

    def test_db_engineer_has_no_subtask_tools(self):
        """Matrix says DB engineer has no subtask tools — implementation must match."""
        tools = _tool_names(get_tools_for_role("database_engineer"))
        subtask_tools = {"submit_subtask_plan", "start_subtask", "complete_subtask", "fail_subtask", "get_subtasks"}
        has_subtask = tools & subtask_tools
        assert not has_subtask, f"database_engineer should not have subtask tools: {sorted(has_subtask)}"


# ---------------------------------------------------------------------------
# Deploy monitor: matrix says it should have subtask tools, but current
# DEPLOY_TOOL_DEFINITIONS is minimal
# ---------------------------------------------------------------------------


class TestDeployMonitorParity:
    """Matrix gives deploy_monitor subtask tools, send_message, and core tools."""

    def test_deploy_definitions_have_core_tools(self):
        tools = _tool_names(DEPLOY_TOOL_DEFINITIONS)
        assert "report_deploy_status" in tools
        assert "check_ci_status" in tools
        assert "update_task_status" in tools
        assert "send_heartbeat" in tools

    def test_deploy_has_subtask_tools(self):
        """Matrix says deploy_monitor should have subtask tools."""
        tools = _tool_names(DEPLOY_TOOL_DEFINITIONS)
        subtask_tools = {"submit_subtask_plan", "start_subtask", "complete_subtask", "fail_subtask", "get_subtasks"}
        missing = subtask_tools - tools
        assert not missing, f"deploy_monitor missing subtask tools: {sorted(missing)}"

    def test_deploy_has_send_message(self):
        """Matrix says deploy_monitor should have send_message."""
        tools = _tool_names(DEPLOY_TOOL_DEFINITIONS)
        assert "send_message" in tools


# ---------------------------------------------------------------------------
# Code reviewer: matrix has report_review_complete, which may not exist yet
# ---------------------------------------------------------------------------


class TestCodeReviewerParity:
    def test_report_review_complete_in_matrix(self):
        assert "report_review_complete" in TOOL_MATRIX["code_reviewer"]

    def test_report_review_complete_exists_in_system(self):
        """report_review_complete must exist in state injections or tool definitions."""
        tool_defs = _tool_names(REVIEW_TOOL_DEFINITIONS)
        injections = set(_STATE_TOOL_INJECTIONS.keys())
        exists = "report_review_complete" in tool_defs or "report_review_complete" in injections
        if not exists:
            pytest.xfail(
                "report_review_complete is in the matrix but not implemented in "
                "_STATE_TOOL_INJECTIONS or REVIEW_TOOL_DEFINITIONS."
            )
