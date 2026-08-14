"""Tool definitions and execution for the LiteLLM agent loop.

Tools are defined as OpenAI-compatible function schemas and dispatched
through the git provider abstraction so the agent prompt stays
provider-agnostic.
"""

import json
import logging
import subprocess
from pathlib import Path
from typing import Any

from ...providers.git import GitProviderProtocol, InlineComment
from .args import coerce_line_number

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool definitions (OpenAI function-calling format)
# ---------------------------------------------------------------------------

REVIEW_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_mr_diff",
            "description": "Get the full unified diff of the merge/pull request.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_changed_files",
            "description": "List all files changed in the merge/pull request.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the contents of a file in the repository. Use this to understand context around changed code.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path relative to repo root."},
                    "start_line": {"type": "integer", "description": "Start reading from this line (1-indexed). Optional."},
                    "end_line": {"type": "integer", "description": "Stop reading at this line (inclusive). Optional."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_code",
            "description": "Search the repository for a regex pattern. Returns matching lines with file paths and line numbers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regex pattern to search for."},
                    "glob": {"type": "string", "description": "File glob to restrict search (e.g. '*.py'). Optional."},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files matching a glob pattern in the repository.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob pattern (e.g. 'src/**/*.ts', '*.py')."},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_mr_comments",
            "description": "Get existing review comments on the merge/pull request. Use this to avoid duplicating prior feedback.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "post_inline_comment",
            "description": "Leave a review comment on a specific file and line in the MR/PR. Every comment must explain WHAT is wrong and WHY. Always provide a line number from the diff; default to 1 if no specific line applies.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "File path relative to repo root."},
                    "line": {
                        "type": "integer",
                        "description": "Line number in the new version of the file. Use the most relevant changed line from the diff. Default to 1 if no specific line applies.",
                    },
                    "body": {"type": "string", "description": "The review comment text. Be specific and actionable."},
                    "severity": {
                        "type": "string",
                        "enum": ["critical", "warning", "nit"],
                        "description": "Severity: critical (must fix), warning (should fix), nit (optional).",
                    },
                },
                "required": ["file_path", "line", "body"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_review",
            "description": "Submit the final review verdict with a summary. Call this once at the end after posting inline comments.",
            "parameters": {
                "type": "object",
                "properties": {
                    "verdict": {
                        "type": "string",
                        "enum": ["approve", "request_changes"],
                        "description": "The review verdict.",
                    },
                    "body": {"type": "string", "description": "Review summary following the output format from your instructions."},
                },
                "required": ["verdict", "body"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------


class ToolExecutor:
    """Executes tool calls using a git provider and optional local repo path."""

    def __init__(
        self,
        provider: GitProviderProtocol,
        project_id: str,
        mr_id: str,
        repo_path: str = "",
    ):
        self.provider = provider
        self.project_id = project_id
        self.mr_id = mr_id
        self.repo_path = repo_path
        self.comments_posted: int = 0

    async def execute(self, tool_name: str, arguments: dict) -> str:
        """Dispatch a tool call and return the result as a string."""
        try:
            if tool_name == "get_mr_diff":
                return await self._get_diff()
            if tool_name == "get_changed_files":
                return await self._get_changed_files()
            if tool_name == "read_file":
                return self._read_file(arguments)
            if tool_name == "search_code":
                return self._search_code(arguments)
            if tool_name == "list_files":
                return self._list_files(arguments)
            if tool_name == "get_mr_comments":
                return await self._get_comments()
            if tool_name == "post_inline_comment":
                return await self._post_inline_comment(arguments)
            if tool_name == "submit_review":
                return await self._submit_review(arguments)
            return json.dumps({"error": f"Unknown tool: {tool_name}"})
        except Exception as e:
            logger.error("Tool %s failed: %s", tool_name, e, exc_info=True)
            return json.dumps({"error": str(e)})

    async def _get_diff(self) -> str:
        diff = await self.provider.get_diff(self.project_id, self.mr_id)
        # Truncate very large diffs to avoid blowing context
        if len(diff) > 100_000:
            return diff[:100_000] + "\n\n... [diff truncated at 100k chars — use read_file for full context]"
        return diff

    async def _get_changed_files(self) -> str:
        files = await self.provider.get_changed_files(self.project_id, self.mr_id)
        return json.dumps(files)

    def _read_file(self, args: dict) -> str:
        path = args.get("path", "")
        if not path:
            return json.dumps({"error": "path is required"})

        if not self.repo_path:
            return json.dumps({"error": "No local repo path configured — cannot read files"})

        file_path = Path(self.repo_path) / path
        if not file_path.exists():
            return json.dumps({"error": f"File not found: {path}"})
        if not file_path.is_file():
            return json.dumps({"error": f"Not a file: {path}"})

        lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
        try:
            start = coerce_line_number(args.get("start_line"), "start_line")
            end = coerce_line_number(args.get("end_line"), "end_line")
        except ValueError as e:
            return json.dumps({"error": str(e)})
        if start is not None:
            start = max(1, start) - 1  # convert to 0-indexed
            if end is not None:
                end_idx = end
            else:
                end_idx = len(lines)
            lines = lines[start:end_idx]

        # Cap output
        if len(lines) > 500:
            lines = lines[:500]
            lines.append("... [truncated at 500 lines — use start_line/end_line for specific ranges]")

        return "\n".join(lines)

    def _search_code(self, args: dict) -> str:
        pattern = args.get("pattern", "")
        if not pattern:
            return json.dumps({"error": "pattern is required"})

        if not self.repo_path:
            return json.dumps({"error": "No local repo path configured — cannot search"})

        cmd = ["rg", "--line-number", "--no-heading", "--max-count", "50", pattern]
        glob_filter = args.get("glob")
        if glob_filter:
            cmd.extend(["--glob", glob_filter])
        cmd.append(self.repo_path)

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

    def _list_files(self, args: dict) -> str:
        pattern = args.get("pattern", "")
        if not pattern:
            return json.dumps({"error": "pattern is required"})

        if not self.repo_path:
            return json.dumps({"error": "No local repo path configured"})

        matches = sorted(Path(self.repo_path).glob(pattern))
        # Return relative paths
        base = Path(self.repo_path)
        relative = [str(m.relative_to(base)) for m in matches if m.is_file()]
        if len(relative) > 200:
            relative = relative[:200]
            relative.append(f"... and more (showing first 200 of {len(matches)})")
        return json.dumps(relative)

    async def _get_comments(self) -> str:
        comments = await self.provider.get_comments(self.project_id, self.mr_id)
        return json.dumps(comments)

    async def _post_inline_comment(self, args: dict) -> str:
        file_path = args.get("file_path", "")
        line = args.get("line", 1) or 1
        body = args.get("body", "")
        severity = args.get("severity", "nit")

        if not file_path or not body:
            return json.dumps({"error": "file_path and body are required"})

        prefixed_body = f"**[{severity.upper()}]** {body}"
        comment = InlineComment(file_path=file_path, line=line, body=prefixed_body)
        await self.provider.post_inline_comment(self.project_id, self.mr_id, comment)
        self.comments_posted += 1
        return json.dumps({"posted": True, "file": file_path, "line": line, "total_comments": self.comments_posted})

    async def _submit_review(self, args: dict) -> str:
        verdict = args.get("verdict", "request_changes")
        body = args.get("body", "")
        result = await self.provider.submit_review(self.project_id, self.mr_id, verdict, body)
        return json.dumps(result)


# Backward compat alias
TOOL_DEFINITIONS = REVIEW_TOOL_DEFINITIONS


# ---------------------------------------------------------------------------
# Role-specific tool definitions for job orchestration
# ---------------------------------------------------------------------------


def _fn(name: str, description: str, params: dict | None = None) -> dict[str, Any]:
    """Helper to build a tool definition in OpenAI function-calling format."""
    props = params or {}
    required = list(props.keys())
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": props,
                "required": required,
            },
        },
    }


# Shared tools available to all job-orchestration agents
_HEARTBEAT_TOOL = _fn("send_heartbeat", "Send a heartbeat to indicate the agent is still working.")

_SUBTASK_TOOLS = [
    _fn(
        "submit_subtask_plan",
        "Submit a plan of subtasks for the current task.",
        {
            "subtasks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "description": {"type": "string"},
                    },
                },
                "description": "List of subtask descriptions in order.",
            },
        },
    ),
    _fn(
        "start_subtask",
        "Mark a subtask as started.",
        {
            "subtask_id": {"type": "string", "description": "Subtask ID to start."},
        },
    ),
    _fn(
        "complete_subtask",
        "Mark a subtask as completed.",
        {
            "subtask_id": {"type": "string", "description": "Subtask ID to complete."},
            "result": {"type": "string", "description": "Summary of what was accomplished."},
        },
    ),
    _fn(
        "fail_subtask",
        "Mark a subtask as failed.",
        {
            "subtask_id": {"type": "string", "description": "Subtask ID that failed."},
            "error": {"type": "string", "description": "Error message."},
        },
    ),
    _fn("get_subtasks", "Get all subtasks for the current task."),
]

_SEND_MESSAGE_TOOL = _fn(
    "send_message",
    "Send a message to another agent.",
    {
        "to_role": {"type": "string", "description": "Target agent role (or null for broadcast)."},
        "content": {"type": "string", "description": "Message content."},
    },
)

_UPDATE_TASK_STATUS_TOOL = _fn(
    "update_task_status",
    "Update the status of the current task.",
    {
        "status": {"type": "string", "description": "New task status."},
        "error": {"type": "string", "description": "Error message (if failing)."},
    },
)

# Spec analyst tools (only refines spec, does NOT create tasks)
SPEC_ANALYST_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    _fn(
        "submit_refined_spec",
        "Submit the refined and analyzed specification.",
        {
            "spec": {"type": "string", "description": "The refined specification text."},
        },
    ),
    _SEND_MESSAGE_TOOL,
    _HEARTBEAT_TOOL,
]

# Arbiter tools (creates tasks and coordinates)
ARBITER_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    _fn(
        "submit_refined_spec",
        "Submit the refined and analyzed specification.",
        {
            "spec": {"type": "string", "description": "The refined specification text."},
        },
    ),
    _fn(
        "create_task",
        "Create a new task for the job.",
        {
            "title": {"type": "string", "description": "Short task title."},
            "description": {"type": "string", "description": "Detailed task description."},
            "service": {"type": "string", "description": "Target service name."},
            "agent_role": {"type": "string", "description": "Agent role to handle this task."},
        },
    ),
    _fn("mark_tasks_created", "Signal that all tasks have been created for this spec."),
    _SEND_MESSAGE_TOOL,
    _HEARTBEAT_TOOL,
]

# Engineer tools
ENGINEER_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    _fn(
        "read_file",
        "Read a file from the repository.",
        {
            "path": {"type": "string", "description": "File path relative to repo root."},
            "start_line": {"type": "integer", "description": "Start line (1-indexed, optional)."},
            "end_line": {"type": "integer", "description": "End line (inclusive, optional)."},
        },
    ),
    _fn(
        "write_file",
        "Write content to a file.",
        {
            "path": {"type": "string", "description": "File path relative to repo root."},
            "content": {"type": "string", "description": "File content to write."},
        },
    ),
    _fn(
        "run_command",
        "Run a shell command in the working directory.",
        {
            "command": {"type": "string", "description": "Shell command to execute."},
            "timeout": {"type": "integer", "description": "Timeout in seconds (default 60)."},
        },
    ),
    _fn(
        "search_code",
        "Search the repository for a regex pattern.",
        {
            "pattern": {"type": "string", "description": "Regex pattern to search."},
            "glob": {"type": "string", "description": "File glob filter (optional)."},
        },
    ),
    _fn(
        "create_branch",
        "Create and checkout a new git branch.",
        {
            "branch_name": {"type": "string", "description": "Branch name to create."},
        },
    ),
    _fn(
        "commit",
        "Stage and commit changes.",
        {
            "message": {"type": "string", "description": "Commit message."},
            "files": {"type": "array", "items": {"type": "string"}, "description": "Files to stage (empty = all)."},
        },
    ),
    _fn(
        "push",
        "Push the current branch to remote.",
        {
            "branch_name": {"type": "string", "description": "Branch name to push."},
        },
    ),
    _fn(
        "create_pr",
        "Create a pull request.",
        {
            "title": {"type": "string", "description": "PR title."},
            "body": {"type": "string", "description": "PR description."},
            "base": {"type": "string", "description": "Base branch (default: main)."},
        },
    ),
    _fn(
        "report_pr",
        "Report that a PR has been created for the current task.",
        {
            "pr_url": {"type": "string", "description": "URL of the created PR."},
            "pr_number": {"type": "integer", "description": "PR number."},
            "branch_name": {"type": "string", "description": "Branch name."},
        },
    ),
    _fn(
        "report_no_work_needed",
        (
            "Report that the change this task asks for is ALREADY PRESENT in the codebase. "
            "Use it only after reading the code and confirming that — not when the task is hard, "
            "blocked, or partly done. Closes the task with no PR. Prefer this over inventing a "
            "docs-only change to justify the run."
        ),
        {
            "reason": {
                "type": "string",
                "description": "Concrete evidence: the file, symbol or commit that already satisfies the task, so a human can check the claim.",
            },
        },
    ),
    *_SUBTASK_TOOLS,
    _UPDATE_TASK_STATUS_TOOL,
    _HEARTBEAT_TOOL,
]

# Finisher: the git sequence only.
#
# Derived from ENGINEER_TOOL_DEFINITIONS by name rather than redeclared, so a
# schema change to create_pr or report_pr reaches both roles. Redeclaring these
# would let the two drift, and the finisher is the one that has to get report_pr
# exactly right — it is what drives the PR_OPEN transition.
#
# NOT included, deliberately: read_file, write_file, search_code and the subtask
# tools. This agent does not read the codebase or change it. Withholding those
# is most of the point — it is what keeps the context small and the run cheap,
# and it makes "the finisher edited something" impossible rather than merely
# discouraged. run_command stays because the agent must be able to see what it
# is committing (git status/diff --stat) to write an honest PR body.
_FINISHER_TOOL_NAMES = frozenset(
    {
        "run_command",
        "create_branch",
        "commit",
        "push",
        "create_pr",
        "report_pr",
        "update_task_status",
        "send_heartbeat",
    }
)

FINISHER_TOOL_DEFINITIONS: list[dict[str, Any]] = [t for t in ENGINEER_TOOL_DEFINITIONS if t["function"]["name"] in _FINISHER_TOOL_NAMES]


# Database engineer tools (no subtask decomposition, no PR workflow)
DB_ENGINEER_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    _fn(
        "read_file",
        "Read a file from the repository.",
        {
            "path": {"type": "string", "description": "File path relative to repo root."},
            "start_line": {"type": "integer", "description": "Start line (1-indexed, optional)."},
            "end_line": {"type": "integer", "description": "End line (inclusive, optional)."},
        },
    ),
    _fn(
        "write_file",
        "Write content to a file.",
        {
            "path": {"type": "string", "description": "File path relative to repo root."},
            "content": {"type": "string", "description": "File content to write."},
        },
    ),
    _fn(
        "run_command",
        "Run a shell command in the working directory.",
        {
            "command": {"type": "string", "description": "Shell command to execute."},
            "timeout": {"type": "integer", "description": "Timeout in seconds (default 60)."},
        },
    ),
    _fn(
        "search_code",
        "Search the repository for a regex pattern.",
        {
            "pattern": {"type": "string", "description": "Regex pattern to search."},
            "glob": {"type": "string", "description": "File glob filter (optional)."},
        },
    ),
    _fn(
        "create_branch",
        "Create and checkout a new git branch.",
        {
            "branch_name": {"type": "string", "description": "Branch name to create."},
        },
    ),
    _fn(
        "commit",
        "Stage and commit changes.",
        {
            "message": {"type": "string", "description": "Commit message."},
            "files": {"type": "array", "items": {"type": "string"}, "description": "Files to stage (empty = all)."},
        },
    ),
    _fn(
        "push",
        "Push the current branch to remote.",
        {
            "branch_name": {"type": "string", "description": "Branch name to push."},
        },
    ),
    _fn(
        "report_pr",
        "Report that a PR has been created for the current task.",
        {
            "pr_url": {"type": "string", "description": "URL of the created PR."},
            "pr_number": {"type": "integer", "description": "PR number."},
            "branch_name": {"type": "string", "description": "Branch name."},
        },
    ),
    _SEND_MESSAGE_TOOL,
    _UPDATE_TASK_STATUS_TOOL,
    _HEARTBEAT_TOOL,
]

# Deploy monitor tools
DEPLOY_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    _fn(
        "check_ci_status",
        "Check the CI/CD pipeline status.",
        {
            "pr_url": {"type": "string", "description": "PR URL to check CI for."},
        },
    ),
    _fn(
        "report_deploy_status",
        "Report the deployment status.",
        {
            "status": {"type": "string", "description": "Deploy status: deploying, deployed, failed."},
            "detail": {"type": "string", "description": "Status details."},
        },
    ),
    *_SUBTASK_TOOLS,
    _SEND_MESSAGE_TOOL,
    _UPDATE_TASK_STATUS_TOOL,
    _HEARTBEAT_TOOL,
]


# Code reviewer tools for job orchestration (extends REVIEW_TOOL_DEFINITIONS with state tools)
CODE_REVIEWER_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    *REVIEW_TOOL_DEFINITIONS,
    _fn(
        "report_review_complete",
        "Report that a PR review is complete.",
        {
            "verdict": {"type": "string", "enum": ["approved", "changes_requested"], "description": "Review verdict."},
            "feedback": {"type": "string", "description": "Review feedback summary."},
        },
    ),
    _SEND_MESSAGE_TOOL,
    _fn("get_messages", "Get messages sent to this agent."),
    _UPDATE_TASK_STATUS_TOOL,
    _HEARTBEAT_TOOL,
]


# ---------------------------------------------------------------------------
# Memory tools (gated on memory_enabled)
# ---------------------------------------------------------------------------

_MEMORY_TOOLS: list[dict[str, Any]] = [
    _fn(
        "publish_fact",
        "Publish a fact to the project's shared memory. Other agents can query it.",
        {
            "category": {"type": "string", "description": "Fact category (e.g. 'decision', 'observation', 'pattern')."},
            "key": {"type": "string", "description": "Fact key (short identifier)."},
            "value": {"type": "string", "description": "Fact value (the content)."},
            "tags": {"type": "array", "items": {"type": "string"}, "description": "Tags for categorization."},
        },
    ),
    _fn(
        "query_facts",
        "Query shared facts from the project's memory.",
        {
            "category": {"type": "string", "description": "Filter by category (optional)."},
            "tags": {"type": "array", "items": {"type": "string"}, "description": "Filter by tags (optional)."},
            "limit": {"type": "integer", "description": "Max results to return (default 20)."},
        },
    ),
    _fn(
        "create_memory_note",
        "Create a persistent memory note that will be available in future jobs.",
        {
            "content": {"type": "string", "description": "Note content."},
            "tags": {"type": "array", "items": {"type": "string"}, "description": "Tags for categorization."},
            "links": {"type": "array", "items": {"type": "string"}, "description": "Entity names this note relates to (files, modules, concepts)."},
        },
    ),
]


def get_tools_for_role(role: str, memory_enabled: bool = False) -> list[dict[str, Any]]:
    """Return the tool definitions appropriate for the given agent role."""
    if role == "spec_analyst":
        tools = SPEC_ANALYST_TOOL_DEFINITIONS
    elif role == "arbiter":
        tools = ARBITER_TOOL_DEFINITIONS
    elif role in ("backend_engineer", "frontend_engineer"):
        tools = ENGINEER_TOOL_DEFINITIONS
    elif role == "database_engineer":
        tools = DB_ENGINEER_TOOL_DEFINITIONS
    elif role == "code_reviewer":
        tools = CODE_REVIEWER_TOOL_DEFINITIONS
    elif role == "deploy_monitor":
        tools = DEPLOY_TOOL_DEFINITIONS
    elif role == "finisher":
        tools = FINISHER_TOOL_DEFINITIONS
    else:
        tools = ENGINEER_TOOL_DEFINITIONS

    if memory_enabled:
        tools = tools + _MEMORY_TOOLS
    return tools
