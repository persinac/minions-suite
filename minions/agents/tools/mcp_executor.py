"""In-process tool executor that routes state tools through the MCP server.

State-mutating tools (submit_refined_spec, create_task, etc.) are dispatched
directly to the MCP server's Python functions via FastMCP.get_tool(), keeping
server.py as the single source of truth. Local tools (read_file, write_file,
run_command, git ops) are handled by built-in methods that operate on the
agent's working directory.

This module is only used for in-process agent execution. K8s workers
communicate with the MCP server over HTTP (separate path).
"""

import asyncio
import json
import logging
import subprocess
from pathlib import Path

from ...core.models import AgentRole, Job, Task

logger = logging.getLogger(__name__)

# State tools that route through the MCP server, mapped to the context
# parameters that must be injected before calling the MCP function.
# Format: tool_name -> list of (param_name, context_attr) pairs to inject.
_STATE_TOOL_INJECTIONS: dict[str, list[tuple[str, str]]] = {
    "submit_refined_spec": [("job_id", "job_id")],
    "create_task": [("job_id", "job_id")],
    "mark_tasks_created": [("job_id", "job_id")],
    "mark_phases_created": [("job_id", "job_id")],
    "create_phase_card": [("job_id", "job_id")],
    "create_trello_tech_debt": [("job_id", "job_id")],
    "update_task_status": [("task_id", "task_id")],
    "report_pr": [("task_id", "task_id")],
    "report_deploy_status": [("task_id", "task_id")],
    "submit_subtask_plan": [("task_id", "task_id")],
    "get_subtasks": [("task_id", "task_id")],
    "send_message": [("job_id", "job_id"), ("from_role", "agent_role")],
    "send_heartbeat": [
        ("agent_id", "agent_id"),
        ("agent_role", "agent_role"),
        ("job_id", "job_id"),
        ("current_task_id", "task_id"),
    ],
    # These subtask tools need no injection — params come from the LLM directly.
    "start_subtask": [],
    "complete_subtask": [],
    "fail_subtask": [],
    # Review completion
    "report_review_complete": [("task_id", "task_id")],
    # Read-only state tools
    "get_messages": [("job_id", "job_id")],
    "get_job_status": [("job_id", "job_id")],
}

# Local tools handled by built-in methods (filesystem, git, subprocess).
_LOCAL_TOOLS = frozenset(
    {
        "read_file",
        "write_file",
        "run_command",
        "search_code",
        "create_branch",
        "commit",
        "push",
        "create_pr",
        "check_ci_status",
    }
)


class McpToolExecutor:
    """Routes LLM tool calls to MCP server functions or local methods.

    State tools call through to the MCP server's registered tool functions
    (via FastMCP.get_tool().fn) with context parameters injected.
    Local tools operate directly on the filesystem/shell.
    """

    def __init__(
        self,
        mcp_server,
        job_id: str,
        task_id: str,
        agent_id: str,
        agent_role: str,
        working_dir: str = ".",
        config=None,
        project=None,
    ):
        self.mcp_server = mcp_server
        self.job_id = job_id
        self.task_id = task_id
        self.agent_id = agent_id
        self.agent_role = agent_role
        self.working_dir = working_dir
        self.config = config
        self.project = project
        # Cache resolved MCP tool functions
        self._tool_cache: dict[str, object] = {}

    async def execute(self, tool_name: str, arguments: dict) -> str:
        try:
            if tool_name in _STATE_TOOL_INJECTIONS:
                return await self._call_mcp_tool(tool_name, arguments)
            if tool_name in _LOCAL_TOOLS:
                return await self._call_local_tool(tool_name, arguments)
            return json.dumps({"error": f"Unknown tool: {tool_name}"})
        except Exception as e:
            logger.error("McpToolExecutor %s failed: %s", tool_name, e, exc_info=True)
            return json.dumps({"error": str(e)})

    async def _call_mcp_tool(self, tool_name: str, arguments: dict) -> str:
        """Inject context params and call the MCP server tool function directly."""
        enriched = dict(arguments)
        for param_name, context_attr in _STATE_TOOL_INJECTIONS[tool_name]:
            if param_name not in enriched:
                enriched[param_name] = getattr(self, context_attr)

        fn = await self._resolve_tool_fn(tool_name)
        if fn is None:
            return json.dumps({"error": f"MCP tool '{tool_name}' not found on server"})

        result = await fn(**enriched)
        # MCP tool functions return strings already
        if isinstance(result, str):
            return result
        return json.dumps(result, default=str)

    async def _resolve_tool_fn(self, tool_name: str):
        """Get the raw Python function for an MCP tool, with caching."""
        if tool_name in self._tool_cache:
            return self._tool_cache[tool_name]
        try:
            tool = await self.mcp_server.get_tool(tool_name)
            fn = tool.fn
            self._tool_cache[tool_name] = fn
            return fn
        except Exception:
            logger.warning("Could not resolve MCP tool: %s", tool_name, exc_info=True)
            return None

    async def _call_local_tool(self, tool_name: str, arguments: dict) -> str:
        """Dispatch to a built-in local tool method."""
        handler = {
            "read_file": self._read_file,
            "write_file": self._write_file,
            "run_command": self._run_command,
            "search_code": self._search_code,
            "create_branch": self._create_branch,
            "commit": self._commit,
            "push": self._push,
            "create_pr": self._create_pr,
            "check_ci_status": self._check_ci_status,
        }.get(tool_name)
        if handler is None:
            return json.dumps({"error": f"Unknown local tool: {tool_name}"})
        return await handler(arguments)

    # ------------------------------------------------------------------
    # Local tool implementations
    # ------------------------------------------------------------------

    async def _read_file(self, args: dict) -> str:
        path = args.get("path", "")
        if not path:
            return json.dumps({"error": "path is required"})
        file_path = Path(self.working_dir) / path
        if not file_path.exists():
            return json.dumps({"error": f"File not found: {path}"})
        if not file_path.is_file():
            return json.dumps({"error": f"Not a file: {path}"})
        lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
        start = args.get("start_line")
        end = args.get("end_line")
        if start is not None:
            start = max(1, start) - 1
            end_idx = end if end is not None else len(lines)
            lines = lines[start:end_idx]
        if len(lines) > 500:
            lines = lines[:500]
            lines.append("... [truncated at 500 lines — use start_line/end_line for specific ranges]")
        return "\n".join(lines)

    async def _write_file(self, args: dict) -> str:
        path = args.get("path", "")
        content = args.get("content", "")
        if not path:
            return json.dumps({"error": "path is required"})
        file_path = Path(self.working_dir) / path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        return json.dumps({"written": True, "path": path, "bytes": len(content.encode("utf-8"))})

    async def _run_command(self, args: dict) -> str:
        command = args.get("command", "")
        timeout = args.get("timeout", 60)
        if not command:
            return json.dumps({"error": "command is required"})
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.working_dir,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            out = stdout.decode("utf-8", errors="replace")[:20_000]
            err = stderr.decode("utf-8", errors="replace")[:5_000]
            return json.dumps({"returncode": proc.returncode, "stdout": out, "stderr": err})
        except TimeoutError:
            return json.dumps({"error": f"Command timed out after {timeout}s"})
        except Exception as e:
            return json.dumps({"error": str(e)})

    async def _search_code(self, args: dict) -> str:
        pattern = args.get("pattern", "")
        if not pattern:
            return json.dumps({"error": "pattern is required"})
        cmd = ["rg", "--line-number", "--no-heading", "--max-count", "50", pattern]
        glob_filter = args.get("glob")
        if glob_filter:
            cmd.extend(["--glob", glob_filter])
        cmd.append(self.working_dir)
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            if result.returncode == 0:
                return result.stdout[:20_000]
            if result.returncode == 1:
                return "No matches found."
            return json.dumps({"error": f"rg failed: {result.stderr[:200]}"})
        except FileNotFoundError:
            return json.dumps({"error": "rg (ripgrep) not installed"})
        except subprocess.TimeoutExpired:
            return json.dumps({"error": "Search timed out after 15s"})

    async def _create_branch(self, args: dict) -> str:
        branch = args.get("branch_name", "")
        if not branch:
            return json.dumps({"error": "branch_name is required"})
        proc = await asyncio.create_subprocess_exec(
            "git",
            "checkout",
            "-b",
            branch,
            cwd=self.working_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            return json.dumps({"error": f"git checkout -b failed: {stderr.decode()[:300]}"})
        return json.dumps({"branch": branch, "created": True})

    async def _commit(self, args: dict) -> str:
        message = args.get("message", "")
        files = args.get("files", [])
        if not message:
            return json.dumps({"error": "message is required"})
        if files:
            stage_cmd = ["git", "add", *files]
        else:
            stage_cmd = ["git", "add", "-A"]
        proc = await asyncio.create_subprocess_exec(
            *stage_cmd,
            cwd=self.working_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        proc = await asyncio.create_subprocess_exec(
            "git",
            "commit",
            "-m",
            message,
            cwd=self.working_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            return json.dumps({"error": f"git commit failed: {stderr.decode()[:300]}"})
        return json.dumps({"committed": True, "message": message})

    async def _push(self, args: dict) -> str:
        branch = args.get("branch_name", "")
        if not branch:
            return json.dumps({"error": "branch_name is required"})
        proc = await asyncio.create_subprocess_exec(
            "git",
            "push",
            "-u",
            "origin",
            branch,
            cwd=self.working_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            return json.dumps({"error": f"git push failed: {stderr.decode()[:300]}"})
        return json.dumps({"pushed": True, "branch": branch})

    async def _create_pr(self, args: dict) -> str:
        title = args.get("title", "")
        body = args.get("body", "")
        base = args.get("base", "main")
        if not title:
            return json.dumps({"error": "title is required"})

        labels = [f"minions-job-{self.job_id[:8]}"]
        provider_type = (self.project.git_provider if self.project else None) or (self.config.git_provider if self.config else "")

        # GitLab: use REST API to create MR with labels
        if provider_type == "gitlab" and self.config and self.project:
            from ...providers.git import create_provider

            # Get current branch name
            proc = await asyncio.create_subprocess_exec(
                "git",
                "rev-parse",
                "--abbrev-ref",
                "HEAD",
                cwd=self.working_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            source_branch = stdout.decode().strip()

            try:
                provider = create_provider(
                    "gitlab",
                    gitlab_url=self.project.gitlab_url or self.config.gitlab_url,
                    token=self.config.gitlab_token,
                )
                result = await provider.create_mr(
                    self.project.project_id,
                    source_branch=source_branch,
                    target_branch=base,
                    title=title,
                    description=body,
                    labels=labels,
                )
                if result.get("created"):
                    return json.dumps({"pr_url": result["mr_url"], "mr_id": result["mr_id"], "created": True})
                return json.dumps({"error": result.get("error", "GitLab MR creation failed")})
            except Exception as e:
                logger.warning("GitLab create_mr failed, falling back to git push: %s", e)

        # GitHub: use gh CLI + add labels after
        proc = await asyncio.create_subprocess_exec(
            "gh",
            "pr",
            "create",
            "--title",
            title,
            "--body",
            body,
            "--base",
            base,
            "--label",
            ",".join(labels),
            cwd=self.working_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            return json.dumps({"error": f"gh pr create failed: {stderr.decode()[:300]}"})
        pr_url = stdout.decode().strip()
        return json.dumps({"pr_url": pr_url, "created": True})

    async def _check_ci_status(self, args: dict) -> str:
        pr_url = args.get("pr_url", "")
        if not pr_url:
            return json.dumps({"error": "pr_url is required"})
        try:
            proc = await asyncio.create_subprocess_exec(
                "gh",
                "pr",
                "checks",
                pr_url,
                "--json",
                "name,state,conclusion",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            if proc.returncode != 0:
                return json.dumps({"error": f"gh pr checks failed: {stderr.decode()[:300]}"})
            return stdout.decode()[:20_000]
        except TimeoutError:
            return json.dumps({"error": "CI status check timed out"})
        except FileNotFoundError:
            return json.dumps({"error": "gh CLI not installed"})


def create_mcp_tool_executor(
    mcp_server,
    job: Job,
    task: Task,
    agent_id: str,
    working_dir: str = ".",
    config=None,
    project=None,
):
    """Create the appropriate tool executor for the given task's agent role.

    Returns None for CODE_REVIEWER — reviewers get their ToolExecutor
    via the provider/mr_info path in run_agent().
    """
    if task.agent_role == AgentRole.CODE_REVIEWER:
        return None

    return McpToolExecutor(
        mcp_server=mcp_server,
        job_id=job.id,
        task_id=task.id,
        agent_id=agent_id,
        agent_role=str(task.agent_role),
        working_dir=working_dir,
        config=config,
        project=project,
    )
