"""The MCP server answers on both HTTP transports at once.

Until now it spoke only HTTP+SSE, the legacy MCP transport. Modern MCP clients
speak streamable HTTP and POST the initialize body straight at the URL, which a
GET-only ``/sse`` endpoint answers with **HTTP 405** and zero tools -- which is
exactly what a containerised KiroCrew's own ``mcp_discovery.probe_server``
returned on 2026-09-20.

Flipping the transport instead of adding it would have stranded every client
already pointed at ``/sse``: the herder panes reach this server through a
user-scope ``~/.claude.json`` entry read at session start, and a herder that
loses its tools mid-task cannot call ``complete_engineer_work``, leaving its
claim open until ``herder_claim_timeout_seconds``.

The regression these tests exist to catch is asymmetric and quiet: a change
that shadows one transport leaves the other perfectly healthy, so a smoke test
against a single endpoint proves nothing. Each transport is therefore exercised
end to end -- handshake, ``list_tools``, and a real ``call_tool`` round trip --
because a route that merely EXISTS can still fail on first use if its lifespan
never ran.
"""

import asyncio
import contextlib
import socket

import pytest
import uvicorn
from fastmcp import Client, FastMCP

from minions.server.transport import SSE_PATH, STREAMABLE_HTTP_PATH, build_dual_transport_app


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_server() -> FastMCP:
    mcp = FastMCP("dual-transport-test")

    @mcp.tool
    def echo(value: str) -> str:
        """Return the argument, proving a full request/response round trip."""
        return f"echo:{value}"

    return mcp


@contextlib.asynccontextmanager
async def _running(app, port: int):
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(200):
            if server.started:
                break
            await asyncio.sleep(0.05)
        else:
            raise RuntimeError("uvicorn did not start")
        yield
    finally:
        server.should_exit = True
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(task, timeout=10)


def test_both_transports_have_routes():
    """Both apps' paths survive composition into one router.

    Two ``Mount("", app=...)`` entries would pass a "the app builds" assertion
    and still 404 on SSE, because Starlette dispatches on the first prefix
    match. Asserting the paths is what separates those two outcomes.
    """
    app = build_dual_transport_app(_make_server())
    paths = {getattr(route, "path", None) for route in app.routes}

    assert STREAMABLE_HTTP_PATH in paths
    assert SSE_PATH in paths


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [STREAMABLE_HTTP_PATH, SSE_PATH])
async def test_transport_serves_tools(path: str):
    """Each transport completes a handshake, lists tools, and calls one.

    ``fastmcp.Client`` infers the transport from the URL -- streamable HTTP
    unless the path ends in ``/sse`` -- so parametrising the path exercises
    genuinely different client transports against one running server.
    """
    port = _free_port()
    app = build_dual_transport_app(_make_server())

    async with _running(app, port), Client(f"http://127.0.0.1:{port}{path}") as client:
        tools = await client.list_tools()
        assert "echo" in {tool.name for tool in tools}

        result = await client.call_tool("echo", {"value": "hi"})
        assert result.content[0].text == "echo:hi"


@pytest.mark.asyncio
async def test_transports_share_one_port():
    """One listener serves both, so neither needs a second port opened."""
    port = _free_port()
    app = build_dual_transport_app(_make_server())

    async with _running(app, port):
        for path in (STREAMABLE_HTTP_PATH, SSE_PATH):
            async with Client(f"http://127.0.0.1:{port}{path}") as client:
                await client.ping()
