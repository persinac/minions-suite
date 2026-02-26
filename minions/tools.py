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

from .git_provider import GitProviderProtocol, InlineComment

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool definitions (OpenAI function-calling format)
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS: list[dict[str, Any]] = [
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
            "description": "Leave a review comment on a specific file and line in the MR/PR. Every comment must explain WHAT is wrong and WHY.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "File path relative to repo root."},
                    "line": {"type": "integer", "description": "Line number in the new version of the file."},
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
        start = args.get("start_line")
        end = args.get("end_line")
        if start is not None:
            start = max(1, start) - 1  # convert to 0-indexed
            end_idx = end if end is not None else len(lines)
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
        line = args.get("line", 0)
        body = args.get("body", "")
        severity = args.get("severity", "nit")

        if not file_path or not body:
            return json.dumps({"error": "file_path and body are required"})

        prefixed_body = f"**[{severity.upper()}]** {body}"
        comment = InlineComment(file_path=file_path, line=line, body=prefixed_body)
        result = await self.provider.post_inline_comment(self.project_id, self.mr_id, comment)
        self.comments_posted += 1
        return json.dumps({"posted": True, "file": file_path, "line": line, "total_comments": self.comments_posted})

    async def _submit_review(self, args: dict) -> str:
        verdict = args.get("verdict", "request_changes")
        body = args.get("body", "")
        result = await self.provider.submit_review(self.project_id, self.mr_id, verdict, body)
        return json.dumps(result)
