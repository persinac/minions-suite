"""Tests for ToolAuditMiddleware."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from minions.tool_audit_middleware import ToolAuditMiddleware


@pytest.fixture
def mock_db():
    db = MagicMock()
    db.record_tool_call = AsyncMock()
    return db


@pytest.fixture
def middleware(mock_db):
    return ToolAuditMiddleware(db=mock_db)


def _make_context(tool_name, arguments=None):
    ctx = MagicMock()
    ctx.message.name = tool_name
    ctx.message.arguments = arguments or {}
    return ctx


class TestToolAuditMiddleware:
    async def test_records_successful_call(self, middleware, mock_db):
        async def fake_next(context):
            return "tool result"

        ctx = _make_context("submit_refined_spec", {"spec": "test"})
        result = await middleware.on_call_tool(ctx, fake_next)
        assert result == "tool result"
        mock_db.record_tool_call.assert_awaited_once()
        call_kwargs = mock_db.record_tool_call.call_args.kwargs
        assert call_kwargs["tool_name"] == "submit_refined_spec"
        assert call_kwargs["error"] is None

    async def test_records_failed_call(self, middleware, mock_db):
        async def fake_next(context):
            raise RuntimeError("DB error")

        ctx = _make_context("create_task")
        with pytest.raises(RuntimeError):
            await middleware.on_call_tool(ctx, fake_next)
        mock_db.record_tool_call.assert_awaited_once()
        call_kwargs = mock_db.record_tool_call.call_args.kwargs
        assert call_kwargs["tool_name"] == "create_task"
        assert "DB error" in call_kwargs["error"]

    async def test_records_duration(self, middleware, mock_db):
        async def fake_next(context):
            return "ok"

        ctx = _make_context("get_messages")
        await middleware.on_call_tool(ctx, fake_next)
        call_kwargs = mock_db.record_tool_call.call_args.kwargs
        assert call_kwargs["duration_ms"] is not None
        assert call_kwargs["duration_ms"] >= 0

    async def test_extracts_job_id_from_args(self, middleware, mock_db):
        async def fake_next(context):
            return "ok"

        ctx = _make_context("update_task_status", {"job_id": "j42", "status": "done"})
        await middleware.on_call_tool(ctx, fake_next)
        call_kwargs = mock_db.record_tool_call.call_args.kwargs
        assert call_kwargs["job_id"] == "j42"

    async def test_result_summary_truncated(self, middleware, mock_db):
        async def fake_next(context):
            return "x" * 5000

        ctx = _make_context("get_messages")
        await middleware.on_call_tool(ctx, fake_next)
        call_kwargs = mock_db.record_tool_call.call_args.kwargs
        assert call_kwargs["result"] is not None
        assert len(call_kwargs["result"]) <= 1000

    async def test_db_error_swallowed(self, middleware, mock_db):
        """DB record failure should not crash the middleware."""
        mock_db.record_tool_call.side_effect = Exception("DB down")

        async def fake_next(context):
            return "ok"

        ctx = _make_context("get_messages")
        result = await middleware.on_call_tool(ctx, fake_next)
        assert result == "ok"

    async def test_passes_through_result(self, middleware, mock_db):
        sentinel = {"data": [1, 2, 3]}

        async def fake_next(context):
            return sentinel

        ctx = _make_context("get_messages")
        result = await middleware.on_call_tool(ctx, fake_next)
        assert result is sentinel
