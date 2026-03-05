"""Tests for ToolExecutor (review tool dispatch to git provider)."""

import json
from unittest.mock import AsyncMock

import pytest

from minions.agents.tools.definitions import ToolExecutor


class MockGitProvider:
    """Minimal mock implementing GitProviderProtocol methods used by ToolExecutor."""

    def __init__(self):
        self.get_diff = AsyncMock(return_value="diff --git a/foo.py b/foo.py\n+hello")
        self.get_changed_files = AsyncMock(return_value=["foo.py", "bar.py"])
        self.get_comments = AsyncMock(return_value=[{"id": 1, "body": "looks good"}])
        self.post_inline_comment = AsyncMock(return_value={"id": 2})
        self.submit_review = AsyncMock(return_value={"approved": True})


@pytest.fixture
def provider():
    return MockGitProvider()


@pytest.fixture
def executor(provider, tmp_path):
    return ToolExecutor(
        provider=provider,
        project_id="team/api",
        mr_id="42",
        repo_path=str(tmp_path),
    )


class TestGetDiff:
    async def test_returns_diff(self, executor, provider):
        result = await executor.execute("get_mr_diff", {})
        assert "diff --git" in result
        provider.get_diff.assert_awaited_once_with("team/api", "42")

    async def test_truncates_large_diff(self, executor, provider):
        provider.get_diff.return_value = "x" * 200_000
        result = await executor.execute("get_mr_diff", {})
        assert len(result) < 200_000
        assert "truncated" in result


class TestGetChangedFiles:
    async def test_returns_json(self, executor, provider):
        result = await executor.execute("get_changed_files", {})
        files = json.loads(result)
        assert files == ["foo.py", "bar.py"]


class TestReadFile:
    async def test_reads_file(self, executor, tmp_path):
        (tmp_path / "test.py").write_text("line1\nline2\nline3")
        result = await executor.execute("read_file", {"path": "test.py"})
        assert "line1" in result
        assert "line3" in result

    async def test_file_not_found(self, executor):
        result = await executor.execute("read_file", {"path": "missing.py"})
        data = json.loads(result)
        assert "error" in data

    async def test_path_required(self, executor):
        result = await executor.execute("read_file", {})
        data = json.loads(result)
        assert "error" in data

    async def test_line_range(self, executor, tmp_path):
        (tmp_path / "test.py").write_text("\n".join(f"line{i}" for i in range(1, 11)))
        result = await executor.execute("read_file", {"path": "test.py", "start_line": 3, "end_line": 5})
        lines = result.split("\n")
        assert lines[0] == "line3"
        assert lines[-1] == "line5"

    async def test_no_repo_path(self, provider):
        executor = ToolExecutor(provider=provider, project_id="p", mr_id="1", repo_path="")
        result = await executor.execute("read_file", {"path": "test.py"})
        data = json.loads(result)
        assert "error" in data

    async def test_truncates_long_file(self, executor, tmp_path):
        (tmp_path / "big.py").write_text("\n".join(f"line{i}" for i in range(1000)))
        result = await executor.execute("read_file", {"path": "big.py"})
        assert "truncated" in result


class TestListFiles:
    async def test_list_files(self, executor, tmp_path):
        (tmp_path / "a.py").touch()
        (tmp_path / "b.py").touch()
        (tmp_path / "c.txt").touch()
        result = await executor.execute("list_files", {"pattern": "*.py"})
        files = json.loads(result)
        assert len(files) == 2

    async def test_pattern_required(self, executor):
        result = await executor.execute("list_files", {})
        data = json.loads(result)
        assert "error" in data


class TestGetComments:
    async def test_returns_comments(self, executor, provider):
        result = await executor.execute("get_mr_comments", {})
        comments = json.loads(result)
        assert len(comments) == 1
        assert comments[0]["body"] == "looks good"


class TestPostInlineComment:
    async def test_posts_comment(self, executor, provider):
        result = await executor.execute("post_inline_comment", {
            "file_path": "foo.py",
            "line": 10,
            "body": "Bug here",
            "severity": "critical",
        })
        data = json.loads(result)
        assert data["posted"] is True
        assert data["total_comments"] == 1
        assert executor.comments_posted == 1

    async def test_increments_counter(self, executor, provider):
        await executor.execute("post_inline_comment", {"file_path": "a.py", "line": 1, "body": "comment 1"})
        await executor.execute("post_inline_comment", {"file_path": "b.py", "line": 2, "body": "comment 2"})
        assert executor.comments_posted == 2

    async def test_missing_fields(self, executor):
        result = await executor.execute("post_inline_comment", {"line": 10})
        data = json.loads(result)
        assert "error" in data

    async def test_severity_prefix(self, executor, provider):
        await executor.execute("post_inline_comment", {
            "file_path": "foo.py", "line": 1, "body": "Unused import", "severity": "warning",
        })
        call_args = provider.post_inline_comment.call_args
        comment = call_args[0][2]  # third positional arg is the InlineComment
        assert "[WARNING]" in comment.body


class TestSubmitReview:
    async def test_submits_review(self, executor, provider):
        result = await executor.execute("submit_review", {"verdict": "approve", "body": "LGTM"})
        data = json.loads(result)
        assert data["approved"] is True
        provider.submit_review.assert_awaited_once_with("team/api", "42", "approve", "LGTM")


class TestUnknownTool:
    async def test_unknown_tool(self, executor):
        result = await executor.execute("nonexistent_tool", {})
        data = json.loads(result)
        assert "error" in data
        assert "Unknown tool" in data["error"]


class TestErrorHandling:
    async def test_provider_error_caught(self, executor, provider):
        provider.get_diff.side_effect = RuntimeError("API timeout")
        result = await executor.execute("get_mr_diff", {})
        data = json.loads(result)
        assert "error" in data
        assert "API timeout" in data["error"]
