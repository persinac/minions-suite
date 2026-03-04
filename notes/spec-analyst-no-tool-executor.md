# BUG: Orchestration agents have no tool executor in-process mode

## Status: Open

## Summary

The `spec_analyst` and `arbiter` agents silently fail when running in-process because no `ToolExecutor` is wired up for their tool calls. Every tool invocation returns `{"error": "No tool executor configured"}`, the LLM gives up after a few turns, and the job engine marks the job as `failed`.

## Root Cause

`job_engine.py:_run_in_process()` (line 530) calls `run_agent()` without passing a `tool_executor`. In `agent.py:run_agent()` (line 83-86), non-reviewer agents skip executor setup and leave it as `None`. The loop at `agent.py:227-230` then returns an error string for every tool call:

```python
if tool_executor:
    result = await tool_executor.execute(fn_name, fn_args)
else:
    result = json.dumps({"error": "No tool executor configured"})
```

## Affected Agents

- **spec_analyst** — tools: `submit_refined_spec`, `create_task`, `mark_tasks_created`, `send_message`
- **arbiter** — same tool set (`SPEC_TOOL_DEFINITIONS`)

## Observed Behavior (job ac6876b9)

1. Spec analyst ran 4 turns, never successfully called `submit_refined_spec` → engine warning: "didn't submit refined spec, advancing with raw spec"
2. Arbiter ran 4 turns, never created any tasks → engine hit "Arbiter created no tasks" → job `failed`
3. Both tasks stuck at `in_progress` in the DB; no subtasks created

## Why It Works in K8s Mode

In K8s dispatch mode, the agent runs as a separate process that connects to the MCP server over HTTP. Tool calls route through the FastMCP server (`server.py`) which has real implementations for `submit_refined_spec`, `create_task`, `mark_tasks_created`, etc. that interact with the DB.

## Fix

Create an orchestration tool executor (similar to `ToolExecutor` for review agents) that routes spec/arbiter tool calls to the DB. The implementations already exist in `server.py` (lines 224-285) — they just need to be callable from in-process mode.

Options:
1. **New `OrchestratorToolExecutor` class** in `tools.py` that takes a `db` reference and implements `submit_refined_spec`, `create_task`, `mark_tasks_created` by calling the same DB methods as `server.py`
2. **Route in-process calls through the local MCP server** via HTTP loopback (heavier, but consistent with K8s path)

Option 1 is simpler and avoids the HTTP overhead.

## Key Files

- `minions/agent.py:83-86, 227-230` — where executor is None and errors are returned
- `minions/job_engine.py:530, 782, 826` — where `_run_in_process` is called without executor
- `minions/tools.py:346-362` — `SPEC_TOOL_DEFINITIONS` (tool schemas)
- `minions/server.py:224-285` — working implementations of the tools (MCP server path)
- `minions/job_engine.py:784-792, 828-842` — post-agent logic that detects the failure
