## Context

The minions-suite orchestrates multi-agent dev jobs through a hand-rolled state machine spanning `job_engine.py` (~607 lines), `dev.py` (~793 lines), and `review.py` (~166 lines). The flow is: spec analyst → arbiter → N engineers (per service) → code reviewer → revision cycles → deploy monitor. State transitions are validated by `state_transitions.py` and enforced by an arbiter NATS service with circuit-breaking.

The inner agent loop (`_agent_loop_generic` in `runner.py`, ~230 lines) is a while-loop calling `litellm.acompletion()` with tool-use, custom wind-down phases (soft warning at 80%, hard tool-blocking at 90%), and per-call cost tracking.

The system uses SQLite (dev) and PostgreSQL (prod) for persistence, NATS JetStream for inter-process messaging, and optionally K8s for agent dispatch.

## Goals / Non-Goals

**Goals:**
- Replace the `_advance()` dispatcher pattern with a LangGraph `StateGraph` that makes job phase transitions explicit and testable
- Wrap individual agent execution as LangGraph subgraphs to gain checkpoint/resume without rewriting the LiteLLM tool-use internals
- Enable fault-tolerant resume: if the engine crashes mid-job, resume from last completed node via LangGraph checkpointer
- Preserve the existing DB schema (`Job`, `Task`, `Agent`, `Subtask`, `Message` tables) as the authoritative state store
- Maintain vendor-agnostic LLM routing via LiteLLM (through `ChatLiteLLM` adapter)
- Keep the arbiter as a separate NATS service for cross-process health monitoring

**Non-Goals:**
- Rewriting the inner `_agent_loop_generic()` tool-use loop as native LangGraph nodes — keep the existing LiteLLM while-loop inside a subgraph wrapper
- Migrating to LangGraph Server or LangGraph Cloud — this stays self-hosted
- Replacing NATS messaging between engine and arbiter with LangGraph state channels
- Changing the MCP server, CLI, webhook, or dashboard interfaces
- Adding new agent roles or capabilities as part of this change

## Decisions

### 1. Hybrid architecture: LangGraph for orchestration, LiteLLM loop for agent execution

**Choice:** Use LangGraph `StateGraph` for the job-level state machine (which phase runs next) and wrap the existing `_agent_loop_generic()` as an opaque node function inside each agent subgraph.

**Alternatives considered:**
- *Full LangGraph*: Model each LLM turn as a graph node. Rejected — the wind-down mechanism (tool blocking at 90%), per-turn cost tracking, and rate-limit retry logic would need extensive reimplementation with no clear benefit. The inner loop is working well.
- *No LangGraph*: Keep the dispatcher pattern. Rejected — the `_advance()` if/elif chain is already hard to follow and will get worse as new roles are added.

**Rationale:** The orchestration layer has the most to gain (explicit graph, checkpointing, testable routing). The agent loop has the least to gain (already a clean while-loop with fine-grained control).

### 2. State schema maps onto existing DB models

**Choice:** Define a `JobGraphState(TypedDict)` that mirrors the key fields from `Job`, `Task`, and `Agent` models. Graph nodes read from and write to the DB — the LangGraph state is a view, not the source of truth.

```python
class JobGraphState(TypedDict):
    job_id: str
    job_status: str
    tasks: list[dict]           # task snapshots from DB
    active_agents: list[dict]   # running agent records
    current_phase: str          # node routing hint
    error: str | None
```

**Alternatives considered:**
- *LangGraph state as source of truth*: Let the graph own all state, sync to DB on completion. Rejected — too much migration risk, breaks dashboard/API queries during execution.
- *Dual-write*: Write to both graph state and DB simultaneously. Rejected — consistency headaches.

**Rationale:** The DB is battle-tested for this workload. LangGraph state provides routing context; DB provides persistence and querying.

### 3. PostgresSaver / SqliteSaver for checkpointing

**Choice:** Use LangGraph's built-in `AsyncPostgresSaver` (prod) and `AsyncSqliteSaver` (dev) checkpointers, reusing the existing DB connection configuration.

**Alternatives considered:**
- *MemorySaver only*: Simpler, but no fault recovery across restarts.
- *Custom checkpointer writing to existing tables*: More control, but reinvents what LangGraph already provides.

**Rationale:** The checkpointer tables are separate from the application tables. No schema conflicts. Fault recovery is a primary motivation for this change.

### 4. Review cycle as a graph cycle (not polling)

**Choice:** Model the engineer → reviewer → revision loop as an explicit graph cycle:

```
engineer_node → pr_open_node → reviewer_node → [conditional]
    ↑                                              |
    |←── revision_node ←── changes_requested ──────|
    |                                              |
    └──────────── approved ── merge_node ──→ END
```

**Alternatives considered:**
- *Keep polling in manage_dev_tasks()*: No change. Rejected — polling is the primary source of complexity in `dev.py`.

**Rationale:** The review cycle is naturally a graph cycle. Making it explicit eliminates the status-polling dispatch in `manage_dev_tasks()` (~150 lines).

### 5. Service-level parallelism via Map-Reduce pattern

**Choice:** Use LangGraph's `Send()` API to fan out engineer subgraphs per service, then fan in at the review gate.

```python
def route_to_engineers(state: JobGraphState) -> list[Send]:
    return [Send("engineer_subgraph", {"task": t}) for t in state["tasks"] if t["status"] == "pending"]
```

**Alternatives considered:**
- *Sequential per-service*: Run one service at a time. Rejected — loses current parallelism.
- *Manual asyncio.create_task()*: Keep current approach. Rejected — doesn't benefit from graph structure.

**Rationale:** `Send()` preserves the current parallel-across-services behavior while making it part of the graph.

### 6. ChatLiteLLM adapter for LLM calls

**Choice:** Use `langchain-community`'s `ChatLiteLLM` wrapper to maintain vendor-agnostic model routing. Cost tracking via LangChain callbacks wrapping `litellm.completion_cost()`.

**Alternatives considered:**
- *Direct LangChain model classes* (ChatAnthropic, ChatOpenAI): Vendor lock-in per model. Rejected.
- *Keep raw litellm.acompletion()*: Works inside the agent loop subgraph, but orchestration-level LLM calls (if any) would need LangChain compatibility.

**Rationale:** The inner agent loop keeps using `litellm.acompletion()` directly (inside the subgraph wrapper). `ChatLiteLLM` is only needed if we add LLM-based routing at the orchestration level later.

## Risks / Trade-offs

- **Dependency weight** → `langgraph` + `langchain-core` + `langchain-community` add ~50+ transitive deps. **Mitigation:** Pin versions strictly; use `uv` lock file; evaluate trimming unused langchain extras.

- **Abstraction tax on debugging** → Graph execution is harder to trace than a while-loop. **Mitigation:** Use `stream_mode="debug"` during development; preserve agent log files; keep dashboard queries against DB (not graph state).

- **Migration surface is large** → `job_engine.py`, `dev.py`, `review.py` all change significantly. **Mitigation:** Phased rollout (see below). Feature flag to switch between old dispatcher and new graph during transition.

- **LangGraph checkpointer schema migrations** → LangGraph's Postgres checkpointer creates its own tables. Future LangGraph upgrades may require migrations. **Mitigation:** Use a separate schema/prefix for checkpointer tables; pin langgraph version.

- **Cost tracking gap** → `ChatLiteLLM` doesn't expose raw completion response needed for `litellm.completion_cost()`. **Mitigation:** Inner agent loop keeps using raw `litellm.acompletion()` directly — cost tracking is unaffected for agent execution. Only orchestration-level calls (if added) would need a callback wrapper.

- **K8s dispatch compatibility** → K8s agents run outside the graph process. **Mitigation:** K8s dispatch becomes an async node that publishes to NATS and awaits result — same as current `_dispatch_k8s()`, just wrapped as a graph node.

## Migration Plan

**Phase 1: Agent subgraph wrapper** (lowest risk)
- Create `AgentSubgraph` that wraps `_agent_loop_generic()` as a single LangGraph node
- Add checkpointer for agent-level fault recovery
- No changes to job orchestration — `run_agent()` still called the same way
- Deploy behind feature flag `USE_LANGGRAPH_AGENT=true`

**Phase 2: Job orchestration graph**
- Replace `_advance()` dispatcher with `JobGraph` StateGraph
- Nodes: `spec_analysis`, `task_decomposition`, `engineer_dispatch`, `review_cycle`, `deploy`, `completion`
- Wire `engineer_dispatch` to fan out `AgentSubgraph` instances per service via `Send()`
- Deploy behind feature flag `USE_LANGGRAPH_ENGINE=true`

**Phase 3: Review cycle graph**
- Replace `manage_dev_tasks()` polling with explicit graph cycle
- Conditional edges: `approved` → merge, `changes_requested` → revision
- Remove `review_status` polling from engine poll loop

**Phase 4: Cleanup**
- Remove old dispatcher code and feature flags
- Update tests to use graph-based orchestration
- Document new architecture

**Rollback:** Each phase is behind a feature flag. Disable flag → falls back to existing code path. No DB schema changes needed for rollback.

## Open Questions

- Should the arbiter eventually become a LangGraph node (removing the NATS dependency for in-process deployments), or should it remain a separate service for all deployment modes?
- What is the performance impact of LangGraph's checkpointer writes on the hot path (every node transition)?
- Should we adopt LangGraph's `Store` API for cross-job memory (e.g., agent learning from prior reviews), or keep using the existing `Message` table?
