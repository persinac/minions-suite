## Why

The job orchestration engine (`engine/dev.py`, `engine/review.py`, `engine/job_engine.py`) uses a hand-rolled state machine with dispatcher if/elif chains, fire-and-forget coroutine spawning, and manual retry/recovery logic spread across ~1,600 lines. This makes the multi-agent flow (spec analyst → arbiter → engineers → reviewer → revision cycles) hard to reason about, test in isolation, and extend with new agent roles. LangGraph provides a graph-based execution model with built-in checkpointing, fault recovery, and subgraph composition that maps directly onto the existing state machine — formalizing what's already implicit.

## What Changes

- **Replace the `_advance()` dispatcher** in `job_engine.py` with a LangGraph `StateGraph` where each job phase (spec analysis, task decomposition, engineer execution, review cycle, deploy) is an explicit node with typed conditional edges.
- **Wrap `_agent_loop_generic()`** as a LangGraph subgraph per agent role, preserving the existing LiteLLM tool-use loop internally while gaining checkpoint/resume and structured state management externally.
- **Replace manual retry/recovery logic** (attempt counters, orphan detection, PENDING resets) with LangGraph's `RetryPolicy` and checkpoint-based resume.
- **Formalize the review cycle** (PR_OPEN → IN_REVIEW → changes_requested → revision) as a graph cycle with explicit conditional edges instead of status polling in `manage_dev_tasks()`.
- **Add `langgraph`, `langchain-core`, `langchain-community`** as dependencies; use `ChatLiteLLM` to maintain vendor-agnostic model routing.
- **Preserve existing DB schema** — `Job`, `Task`, `Agent`, `Subtask`, `Message` tables remain the source of truth. LangGraph state maps onto these models, not the other way around.
- **Keep the arbiter as a separate NATS service** — LangGraph manages in-process orchestration; the arbiter continues to handle cross-process health monitoring and circuit-breaking.

## Capabilities

### New Capabilities
- `graph-orchestration`: LangGraph-based job orchestration graph replacing the dispatcher pattern in job_engine.py and dev.py
- `agent-subgraph`: Reusable LangGraph subgraph wrapping the LiteLLM tool-use loop for each agent role (engineer, reviewer, spec analyst, arbiter)
- `checkpoint-recovery`: LangGraph checkpointer integration (PostgresSaver for prod, SqliteSaver for dev) enabling fault-tolerant resume from last successful node

### Modified Capabilities
<!-- No existing specs to modify -->

## Impact

- **Code**: Major refactor of `engine/job_engine.py` (dispatcher → graph), `engine/dev.py` (handler functions → graph nodes), `agents/runner.py` (loop → subgraph). `engine/review.py`, `engine/arbiter.py`, `engine/deploy.py` adapted as graph nodes.
- **Dependencies**: Adds `langgraph>=1.1`, `langchain-core>=0.3`, `langchain-community>=0.3` to `pyproject.toml`. Significant new dependency tree.
- **APIs**: Internal only — no changes to MCP server, CLI, or webhook interfaces. Dashboard queries remain unchanged (same DB tables).
- **Testing**: Existing 372 tests will need updates for the new orchestration layer. LangGraph graphs are unit-testable in isolation (invoke with mock state), which should improve test coverage of state transitions.
- **Performance**: Minimal overhead — LangGraph adds graph dispatch latency (~ms) per node transition. The LLM calls and tool execution dominate runtime.
- **Risk**: Large refactor surface. Recommend phased rollout: inner agent loop first (lowest risk), then job orchestration, then review cycle.
