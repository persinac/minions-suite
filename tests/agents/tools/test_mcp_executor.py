"""Tests for McpToolExecutor (MCP server tool routing + local tools)."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from minions.agents.tools.mcp_executor import (
    _LOCAL_TOOLS,
    _STATE_TOOL_INJECTIONS,
    McpToolExecutor,
    create_mcp_tool_executor,
)
from minions.core.models import AgentRole, Job, Task

# -----------------------------------------------------------------------
# State tool injection mapping
# -----------------------------------------------------------------------


class TestStateToolInjections:
    def test_submit_refined_spec_injects_job_id(self):
        injections = _STATE_TOOL_INJECTIONS["submit_refined_spec"]
        assert ("job_id", "job_id") in injections

    def test_update_task_status_injects_task_id(self):
        injections = _STATE_TOOL_INJECTIONS["update_task_status"]
        assert ("task_id", "task_id") in injections

    def test_send_message_injects_job_and_role(self):
        injections = _STATE_TOOL_INJECTIONS["send_message"]
        param_names = [p[0] for p in injections]
        assert "job_id" in param_names
        assert "from_role" in param_names

    def test_send_heartbeat_injects_all_context(self):
        injections = _STATE_TOOL_INJECTIONS["send_heartbeat"]
        param_names = [p[0] for p in injections]
        assert "agent_id" in param_names
        assert "agent_role" in param_names
        assert "job_id" in param_names
        assert "current_task_id" in param_names

    def test_subtask_tools_no_injection(self):
        for tool in ["start_subtask", "complete_subtask", "fail_subtask"]:
            assert _STATE_TOOL_INJECTIONS[tool] == []

    def test_all_state_tools_in_map(self):
        expected_tools = {
            "submit_refined_spec",
            "create_task",
            "mark_tasks_created",
            "mark_phases_created",
            "create_phase_card",
            "create_trello_tech_debt",
            "update_task_status",
            "report_pr",
            "report_deploy_status",
            "submit_subtask_plan",
            "get_subtasks",
            "start_subtask",
            "complete_subtask",
            "fail_subtask",
            "send_message",
            "send_heartbeat",
            "get_messages",
            "get_job_status",
            "report_review_complete",
            # Memory tools (gated on memory_enabled at runtime)
            "publish_fact",
            "query_facts",
            "create_memory_note",
        }
        assert expected_tools == set(_STATE_TOOL_INJECTIONS.keys())


class TestLocalTools:
    def test_expected_local_tools(self):
        expected = {"read_file", "write_file", "run_command", "search_code", "create_branch", "commit", "push", "create_pr", "check_ci_status"}
        assert expected == _LOCAL_TOOLS


# -----------------------------------------------------------------------
# MCP tool dispatch
# -----------------------------------------------------------------------


class TestMcpToolDispatch:
    @pytest.fixture
    def mock_mcp(self):
        mcp = MagicMock()
        tool_mock = MagicMock()
        tool_mock.fn = AsyncMock(return_value='{"ok": true}')
        mcp.get_tool = AsyncMock(return_value=tool_mock)
        return mcp

    @pytest.fixture
    def executor(self, mock_mcp, tmp_path):
        return McpToolExecutor(
            mcp_server=mock_mcp,
            job_id="j1",
            task_id="t1",
            agent_id="a1",
            agent_role="backend_engineer",
            working_dir=str(tmp_path),
        )

    async def test_state_tool_injects_job_id(self, executor, mock_mcp):
        await executor.execute("submit_refined_spec", {"spec": "test"})
        tool = await mock_mcp.get_tool("submit_refined_spec")
        call_args = tool.fn.call_args
        assert call_args.kwargs.get("job_id") == "j1" or ("job_id" in (call_args.kwargs if call_args.kwargs else {}))

    async def test_state_tool_does_not_override_explicit_param(self, executor, mock_mcp):
        await executor.execute("submit_refined_spec", {"spec": "test", "job_id": "explicit"})
        tool = await mock_mcp.get_tool("submit_refined_spec")
        call_args = tool.fn.call_args
        assert "explicit" in str(call_args)

    async def test_unknown_tool_returns_error(self, executor):
        result = await executor.execute("totally_unknown", {})
        data = json.loads(result)
        assert "error" in data
        assert "Unknown tool" in data["error"]

    async def test_mcp_tool_not_found(self, executor, mock_mcp):
        mock_mcp.get_tool.side_effect = Exception("not found")
        result = await executor.execute("submit_refined_spec", {"spec": "test"})
        data = json.loads(result)
        assert "error" in data

    async def test_mcp_tool_exception_caught(self, executor, mock_mcp):
        tool = await mock_mcp.get_tool("submit_refined_spec")
        tool.fn.side_effect = RuntimeError("DB error")
        result = await executor.execute("submit_refined_spec", {"spec": "test"})
        data = json.loads(result)
        assert "error" in data


# -----------------------------------------------------------------------
# Local tools
# -----------------------------------------------------------------------


class TestLocalToolExecution:
    @pytest.fixture
    def executor(self, tmp_path):
        return McpToolExecutor(
            mcp_server=MagicMock(),
            job_id="j1",
            task_id="t1",
            agent_id="a1",
            agent_role="backend_engineer",
            working_dir=str(tmp_path),
        )

    async def test_read_file(self, executor, tmp_path):
        (tmp_path / "hello.txt").write_text("hello world")
        result = await executor.execute("read_file", {"path": "hello.txt"})
        assert "hello world" in result

    async def test_read_file_not_found(self, executor):
        result = await executor.execute("read_file", {"path": "missing.txt"})
        data = json.loads(result)
        assert "error" in data

    async def test_read_file_no_path(self, executor):
        result = await executor.execute("read_file", {})
        data = json.loads(result)
        assert "error" in data

    async def test_write_file(self, executor, tmp_path):
        result = await executor.execute("write_file", {"path": "output.txt", "content": "written"})
        data = json.loads(result)
        assert data["written"] is True
        assert (tmp_path / "output.txt").read_text() == "written"

    async def test_write_file_creates_dirs(self, executor, tmp_path):
        await executor.execute("write_file", {"path": "sub/dir/file.txt", "content": "nested"})
        assert (tmp_path / "sub" / "dir" / "file.txt").read_text() == "nested"

    async def test_run_command(self, executor):
        result = await executor.execute("run_command", {"command": "echo hello"})
        data = json.loads(result)
        assert data["returncode"] == 0
        assert "hello" in data["stdout"]

    async def test_run_command_timeout(self, executor):
        result = await executor.execute("run_command", {"command": "sleep 10", "timeout": 1})
        data = json.loads(result)
        assert "error" in data
        assert "timed out" in data["error"]

    async def test_run_command_no_command(self, executor):
        result = await executor.execute("run_command", {})
        data = json.loads(result)
        assert "error" in data

    async def test_read_file_with_line_range(self, executor, tmp_path):
        (tmp_path / "lines.txt").write_text("\n".join(f"line{i}" for i in range(1, 11)))
        result = await executor.execute("read_file", {"path": "lines.txt", "start_line": 3, "end_line": 5})
        lines = result.split("\n")
        assert lines[0] == "line3"


# -----------------------------------------------------------------------
# Factory function
# -----------------------------------------------------------------------


class TestCreateMcpToolExecutor:
    def test_returns_executor_for_engineer(self):
        job = Job(spec="test")
        task = Task(job_id=job.id, title="T", service="api", agent_role=AgentRole.BACKEND_ENGINEER)
        executor = create_mcp_tool_executor(MagicMock(), job, task, "a1")
        assert isinstance(executor, McpToolExecutor)
        assert executor.job_id == job.id
        assert executor.task_id == task.id

    def test_returns_none_for_reviewer(self):
        job = Job(spec="test")
        task = Task(job_id=job.id, title="T", service="api", agent_role=AgentRole.CODE_REVIEWER)
        executor = create_mcp_tool_executor(MagicMock(), job, task, "a1")
        assert executor is None

    def test_returns_executor_for_spec_analyst(self):
        job = Job(spec="test")
        task = Task(job_id=job.id, title="T", service="api", agent_role=AgentRole.SPEC_ANALYST)
        executor = create_mcp_tool_executor(MagicMock(), job, task, "a1")
        assert isinstance(executor, McpToolExecutor)

    def test_returns_executor_for_deploy_monitor(self):
        job = Job(spec="test")
        task = Task(job_id=job.id, title="T", service="api", agent_role=AgentRole.DEPLOY_MONITOR)
        executor = create_mcp_tool_executor(MagicMock(), job, task, "a1")
        assert isinstance(executor, McpToolExecutor)
