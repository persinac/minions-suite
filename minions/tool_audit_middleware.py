"""FastMCP middleware that audits all tool calls to logger and database."""

import json
import logging
import time
from typing import Any

from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

logger = logging.getLogger(__name__)


class ToolAuditMiddleware(Middleware):
    def __init__(self, db):
        self.db = db

    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext) -> Any:
        message = context.message
        tool_name = message.name
        args = message.arguments or {}

        job_id = args.get("job_id")

        args_summary = json.dumps(args, default=str)
        if len(args_summary) > 500:
            args_summary = args_summary[:500] + "..."
        logger.info("TOOL CALL: %s(%s)", tool_name, args_summary)

        start = time.monotonic()
        error_text = None
        result_summary = None
        elapsed_ms = 0.0
        try:
            result = await call_next(context)
            elapsed_ms = (time.monotonic() - start) * 1000

            if result is not None:
                result_str = str(result)
                result_summary = result_str[:1000] if len(result_str) > 1000 else result_str
            logger.info("TOOL DONE: %s (%.0fms)", tool_name, elapsed_ms)
            return result
        except Exception as exc:
            elapsed_ms = (time.monotonic() - start) * 1000
            error_text = str(exc)[:500]
            logger.error("TOOL FAIL: %s (%.0fms) error=%s", tool_name, elapsed_ms, error_text)
            raise
        finally:
            try:
                await self.db.record_tool_call(
                    tool_name=tool_name,
                    params=args if args else None,
                    result=result_summary,
                    error=error_text,
                    duration_ms=elapsed_ms,
                    job_id=job_id,
                )
            except Exception:
                logger.debug("Failed to record tool call to DB", exc_info=True)
