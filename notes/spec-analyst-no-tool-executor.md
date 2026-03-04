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

**Every non-reviewer agent running in-process is broken.** The only agent that gets a working executor is `code_reviewer` — and only when launched via the CLI/webhook path that passes `provider` + `mr_info`. The job engine's `_run_in_process()` never passes these, so even code_reviewer is broken when launched from the dev pipeline's `_run_task_review()`.

| Agent | Tool Set | In-Process Status |
|-------|----------|-------------------|
| `spec_analyst` | `SPEC_TOOL_DEFINITIONS` (`submit_refined_spec`, `create_task`, `mark_tasks_created`, `send_message`) | **Broken** — no executor |
| `arbiter` | `SPEC_TOOL_DEFINITIONS` (same as above) | **Broken** — no executor |
| `backend_engineer` | `ENGINEER_TOOL_DEFINITIONS` (`read_file`, `write_file`, `run_command`, `search_code`, `create_branch`, `commit`, `push`, `create_pr`, `report_pr`, subtask tools) | **Broken** — no executor |
| `frontend_engineer` | `ENGINEER_TOOL_DEFINITIONS` (same) | **Broken** — no executor |
| `database_engineer` | `ENGINEER_TOOL_DEFINITIONS` (same) | **Broken** — no executor |
| `code_reviewer` | `REVIEW_TOOL_DEFINITIONS` | **Broken when launched from `_run_task_review()`** — `_run_in_process` doesn't pass `provider`/`mr_info`, so `run_agent()` skips executor setup. Works only via CLI one-shot path. |
| `deploy_monitor` | `DEPLOY_TOOL_DEFINITIONS` (`check_ci_status`, `report_deploy_status`) | **Broken** — no executor |

### Why code_reviewer works from CLI but not from the dev pipeline

The CLI one-shot review path (`cli.py` → `run_agent()`) passes `provider` and `mr_info` directly, which triggers the executor setup at `agent.py:70-81`. But when `_run_task_review()` in the job engine launches a reviewer, it goes through `_run_in_process()` (line 1013) which doesn't pass those args.

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

- `minions/agent.py:70-86, 227-230` — executor setup (reviewer only) and the None fallback
- `minions/job_engine.py:520-538` — `_run_in_process()` never passes executor, provider, or mr_info
- `minions/job_engine.py:782` — spec_analyst launch (no executor)
- `minions/job_engine.py:826` — arbiter launch (no executor)
- `minions/job_engine.py:941` — engineer launch (no executor)
- `minions/job_engine.py:1013` — code_reviewer from dev pipeline (no executor — missing provider/mr_info)
- `minions/job_engine.py:1186` — deploy_monitor launch (no executor)
- `minions/tools.py:346-419` — all tool definition sets (SPEC, ENGINEER, DEPLOY, REVIEW)
- `minions/server.py:224-285` — working MCP implementations of spec/orchestration tools

## Scope of Fix

The fix needs to handle 4 distinct tool sets:

1. **SPEC tools** (spec_analyst, arbiter) — need an `OrchestratorToolExecutor` that calls DB methods for `submit_refined_spec`, `create_task`, `mark_tasks_created`
2. **ENGINEER tools** — need a `DevToolExecutor` that handles filesystem ops (`read_file`, `write_file`, `run_command`), git ops (`create_branch`, `commit`, `push`, `create_pr`), and task tracking (`report_pr`, subtasks, `update_task_status`)
3. **REVIEW tools** — the existing `ToolExecutor` works, but `_run_in_process` needs to pass `provider`/`mr_info` when launching reviewers from the dev pipeline
4. **DEPLOY tools** — need a `DeployToolExecutor` for `check_ci_status` and `report_deploy_status`
