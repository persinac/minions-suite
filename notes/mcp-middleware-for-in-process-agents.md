# Plan: Route in-process tool calls through MCP middleware

**Status:** Planned
**Depends on:** `mcp_tool_executor.py` (completed — see `bug-tool-calls` branch)

## Context

`McpToolExecutor._call_mcp_tool()` currently calls `tool.fn(**args)` directly — bypassing the FastMCP middleware chain. This means in-process agents don't get:

- **Audit logging** (`ToolAuditMiddleware` records every call to DB + logger)
- Any future middleware (rate limiting, circuit breaking, etc.)

External MCP clients (SSE/HTTP) get middleware automatically because FastMCP runs the chain on every `tools/call` request. In-process agents skip it entirely.

## Problem

`FastMCP._call_tool_middleware(name, args)` requires an active `fastmcp.server.context.Context`. This context is normally created by the MCP transport layer when handling an incoming request. In-process calls have no transport — we need to create the context ourselves.

Verified experimentally:

```python
# Without context:
await mcp._call_tool_middleware('my_tool', {'x': 'world'})
# => RuntimeError: No active context found.

# With context wrapper:
async with fastmcp.server.context.Context(fastmcp=mcp):
    result = await mcp._call_tool_middleware('my_tool', {'x': 'world'})
# => middleware fires, ToolResult returned with .content[0].text
```

## Design

### Approach: Wrap state tool calls in Context + `_call_tool_middleware`

Replace the current direct function call:

```python
# BEFORE (bypasses middleware)
fn = await self._resolve_tool_fn(tool_name)
result = await fn(**enriched)
```

With the middleware-routed call:

```python
# AFTER (runs full middleware chain)
async with fastmcp.server.context.Context(fastmcp=self.mcp_server):
    tool_result = await self.mcp_server._call_tool_middleware(tool_name, enriched)
return _extract_text(tool_result)
```

### Return value translation

- `tool.fn()` returns a raw string (what the MCP tool function returns)
- `_call_tool_middleware()` returns a `ToolResult` with `.content` list of `TextContent` blocks

Need a helper to extract the text:

```python
def _extract_text(tool_result) -> str:
    """Pull text from a FastMCP ToolResult."""
    for block in tool_result.content:
        if hasattr(block, "text"):
            return block.text
    return json.dumps({"error": "No text content in tool result"})
```

### Tool cache removal

Currently caching `tool.fn` references in `_tool_cache`. With middleware routing, no cache needed — `_call_tool_middleware` resolves the tool internally. Remove `_tool_cache` dict and `_resolve_tool_fn()` method.

### Local tools — no middleware

Local tools (read_file, write_file, run_command, git ops) stay as direct method calls. They don't exist on the MCP server and don't need audit middleware — the agent loop already logs every tool call and result to the agent log file.

If local tool auditing is needed later, add a lightweight decorator rather than routing through FastMCP.

## Changes

### 1. Modify `minions/mcp_tool_executor.py`

**Add import:**

```python
import fastmcp.server.context
```

**Replace `_call_mcp_tool()`:**

```python
async def _call_mcp_tool(self, tool_name: str, arguments: dict) -> str:
    """Inject context params and call the MCP server tool via middleware chain."""
    enriched = dict(arguments)
    for param_name, context_attr in _STATE_TOOL_INJECTIONS[tool_name]:
        if param_name not in enriched:
            enriched[param_name] = getattr(self, context_attr)

    async with fastmcp.server.context.Context(fastmcp=self.mcp_server):
        tool_result = await self.mcp_server._call_tool_middleware(tool_name, enriched)

    return _extract_text(tool_result)
```

**Add `_extract_text()` module-level helper:**

```python
def _extract_text(tool_result) -> str:
    """Pull text from a FastMCP ToolResult."""
    for block in tool_result.content:
        if hasattr(block, "text"):
            return block.text
    return json.dumps({"error": "No text content in tool result"})
```

**Remove:**

- `self._tool_cache` dict from `__init__`
- `_resolve_tool_fn()` method

### 2. No changes to other files

| File | Change? | Reason |
|------|---------|--------|
| `tool_audit_middleware.py` | No | Already works — `on_call_tool` fires in the chain |
| `server.py` | No | Still single source of truth |
| `job_engine.py` | No | Passes `mcp_server` to executor, unchanged |
| `cli.py` | No | Creates MCP server and passes to JobEngine, unchanged |

## Call path parity

| Execution path | Middleware runs? | Before | After |
|----------------|-----------------|--------|-------|
| External MCP client (SSE/HTTP) | Yes | Yes | Yes |
| In-process agent (LiteLLM loop) | **Yes** | **No** | **Yes** |
| K8s worker (future, HTTP) | Yes | N/A | N/A |

Every state tool call from every execution path will flow through the same middleware chain.

## Risks

1. **`_call_tool_middleware` is a private API** — FastMCP could change it. Mitigation: pin FastMCP version in `pyproject.toml`; this is a well-structured internal method unlikely to change signature. If it does, the fix is a one-line update.

2. **Context creation overhead** — creating a `Context` per tool call adds minimal overhead (dataclass instantiation + context var set/reset). No network I/O involved.

3. **Error shape change** — `fn()` raises exceptions directly; `_call_tool_middleware` may wrap errors differently depending on `mask_error_details` config. Our MCP tool functions already return error JSON strings rather than raising, so this shouldn't matter in practice. The outer `try/except` in `execute()` catches anything unexpected.

## Verification

1. `uv run ruff check minions/mcp_tool_executor.py` — clean lint
2. `uv run python -c "from minions.mcp_tool_executor import McpToolExecutor; print('OK')"` — imports work
3. Integration test: create FastMCP server with `ToolAuditMiddleware`, call `McpToolExecutor.execute("submit_refined_spec", {"spec": "test"})`, verify middleware `on_call_tool` was invoked (mock `db.record_tool_call` and assert it was called)
4. `task minion:server` starts without errors

## Future middleware candidates

Once this plumbing is in place, adding new middleware is trivial — just `mcp.add_middleware(...)` and it applies to all call paths:

- **Rate limiting** — per-agent or per-tool call rate caps
- **Circuit breaking** — disable tools when DB or external services are unhealthy
- **Cost tracking** — attribute tool call costs to jobs/tenants
- **Tenant isolation** — validate that agents only access their own job/task data
