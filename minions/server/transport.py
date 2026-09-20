"""Serve one FastMCP over both MCP HTTP transports on a single port.

Streamable HTTP (``/mcp``) is the current MCP transport; HTTP+SSE (``/sse``) is
the legacy one it replaced, and the only one this server spoke until now. Both
are served together because the cutover is not ours to schedule:

- Every Claude Code herder pane reaches this server through a *user-scope*
  ``~/.claude.json`` entry pointing at ``/sse``. MCP config is read at session
  start, so flipping the server alone strands a pane that is already running --
  and a herder that loses its tools mid-task cannot call
  ``complete_engineer_work``. Its claim then sits open until
  ``herder_claim_timeout_seconds`` expires.
- ``scripts/herder_trigger.py`` builds a ``fastmcp.Client`` from a URL, and
  fastmcp infers the transport from the path: a URL is streamable HTTP *unless*
  it ends in ``/sse``. So that client migrates by changing one string, with no
  code change -- but only once the server answers on both.

The engine's own agents are unaffected either way: ``McpToolExecutor`` holds a
reference to the server object and calls ``tool.fn()`` in-process, never
crossing the wire. The ``mcp_url`` carried in ``AgentWorkItem`` is constructed
and read by nothing.
"""

import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Any

import uvicorn
from starlette.applications import Starlette

logger = logging.getLogger(__name__)

STREAMABLE_HTTP_PATH = "/mcp"
SSE_PATH = "/sse"


def build_dual_transport_app(mcp: Any) -> Starlette:
    """Compose a FastMCP's streamable-HTTP and SSE apps into one ASGI app.

    The two apps own disjoint paths (``/mcp`` versus ``/sse`` + ``/messages``),
    so their routes compose directly into one router.

    They must NOT be combined as two ``Mount("", app=...)`` entries: Starlette
    dispatches on the first prefix match, so the second app is shadowed
    entirely and its endpoint answers 404 while the first still looks healthy.
    """
    http_app = mcp.http_app(path=STREAMABLE_HTTP_PATH, transport="http")
    sse_app = mcp.http_app(path=SSE_PATH, transport="sse")

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        # Each app's lifespan owns its own session manager and also enters
        # FastMCP's shared ``_lifespan_manager``. Both must run or the
        # transport whose lifespan was skipped fails on its first request.
        # Entering the shared manager twice is safe -- it is guarded by
        # ``_lifespan_result_set`` and the second entry is a plain yield.
        async with http_app.lifespan(app), sse_app.lifespan(app):
            yield

    return Starlette(routes=[*http_app.routes, *sse_app.routes], lifespan=lifespan)


async def serve_dual_transport(mcp: Any, host: str, port: int, log_level: str = "INFO") -> None:
    """Serve ``mcp`` on both HTTP transports until the process is signalled.

    The uvicorn settings mirror ``FastMCP.run_http_async`` so that replacing it
    changes the transports offered and nothing else. ``timeout_graceful_shutdown=0``
    in particular is what makes the server exit promptly on SIGTERM rather than
    waiting out in-flight SSE streams, which never close on their own.

    Note that ``uvicorn.Server.serve()`` installs its own SIGTERM/SIGINT
    handlers, so the diagnostic handlers in ``cli.run_server`` are overridden
    for the duration -- exactly as they already were via ``run_http_async``.
    """
    app = build_dual_transport_app(mcp)
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        timeout_graceful_shutdown=0,
        lifespan="on",
        ws="websockets-sansio",
        log_level=log_level.lower(),
    )
    logger.info(
        "Starting MCP server %r on http://%s:%d%s (streamable HTTP) and http://%s:%d%s (SSE, legacy)",
        getattr(mcp, "name", "minions"),
        host,
        port,
        STREAMABLE_HTTP_PATH,
        host,
        port,
        SSE_PATH,
    )
    await uvicorn.Server(config).serve()
