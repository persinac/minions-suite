# LangGraph Native State Ownership

**Date:** 2026-03-14
**Status:** Proposal
**Goal:** Move from LangGraph-as-thin-router to LangGraph owning the full job/task state machine, with structured parallel execution and checkpoint-based recovery.

---

## Problem

LangGraph is currently a conditional router that wraps existing imperative handlers. The real coordination lives in `manage_dev_tasks()` — a 170-line function with 8 conditional branches that manually tracks task states, spawns fire-and-forget coroutines, detects orphaned agents, and handles revision cycles via if/elif.

Consequences:
- **Checkpoints are shallow:** saving `{job_status: "dev_in_progress"}` doesn't capture which tasks are mid-revision or which agents are running.
- **Crash recovery is manual:** `_startup_cleanup()` scans the DB for orphaned agents and resets tasks — fragile and slow.
- **Poll-loop latency:** 5-second delay between engineer finishing and reviewer launching.
- **Parallel tasks are untracked:** `engine._spawn()` (asyncio.create_task) is fire-and-forget. If the process dies, coroutines are lost.
- **Revision cycle is implicit:** buried in `manage_dev_tasks()` line 613, not visible in any graph or state machine definition.

---

## Architecture: Three Graph Layers

### Layer 1 — Job Graph (exists, minor changes)

Top-level orchestration. Phases run sequentially. The key change: `engineer_dispatch` fans out to parallel task subgraphs via `Send()` instead of spawning fire-and-forget coroutines.

```
START → spec_analysis → task_decomposition → engineer_dispatch
                                                    │
                                          ┌─────────┴─────────┐
                                          │   Send() fan-out   │
                                          │   one per service   │
                                          └─────────┬─────────┘
                                                    │
                                          ┌─────────┴─────────┐
                                          │  collect_results   │  ← join point
                                          └─────────┬─────────┘
                                                    │
                                          route: merged → deploy → completion → END
                                                 failed → fail → END
```

### Layer 2 — Task Subgraph (new, replaces `manage_dev_tasks()`)

One instance per service, running in parallel. Contains the full dev→review→revision cycle as explicit graph edges.

```
  PENDING
     │
     ▼
  run_engineer_node
     │
     ├── has PR? ──────────► run_reviewer_node
     │                             │
     │                      ┌──────┴──────┐
     │                      │             │
     │                   approve    request_changes
     │                      │             │
     │                      ▼             ▼
     │                   MERGED      revision_gate
     │                                    │
     │                             count < max(3)?
     │                              yes │    │ no
     │                                  ▼    ▼
     │                          run_revision  FAILED
     │                          _node
     │                              │
     │                              ▼
     │                          run_reviewer_node  (loop)
     │
     ├── error? ───────────► retry_gate
     │                         │
     │                  attempt < max(3)?
     │                   yes │    │ no
     │                       ▼    ▼
     │                   (loop)  FAILED
     │
     └── db_engineer? ────► DONE (no PR cycle)
```

Role differences handled by conditional edges:
- `database_engineer`: engineer → done (skips PR review entirely)
- `backend_engineer` / `frontend_engineer`: engineer → PR_OPEN → review → revision cycle → merged

### Layer 3 — Agent Subgraph (exists, keep as-is)

Single-node LangGraph wrapper around `_agent_loop_generic()`. The LiteLLM tool-use loop with wind-down is self-contained and doesn't benefit from further graph decomposition.

```
START → agent_execution_node (LiteLLM loop, 100 turns, wind-down) → END
        RetryPolicy: 5 attempts, exponential backoff on rate limits
```

---

## Phased Implementation

### Phase 1: Task Subgraph

**Goal:** Replace `manage_dev_tasks()` with `build_task_graph()`. Make the revision cycle explicit and checkpointable.

**New file:** `engine/task_graph.py`

**State schema:**
```python
class TaskGraphState(TypedDict):
    job_id: str
    task_id: str
    service: str
    agent_role: str
    attempt: int
    max_attempts: int
    revision_count: int
    max_revisions: int
    pr_url: str | None
    branch_name: str | None
    review_verdict: str | None
    review_feedback: str | None
    error: str | None
    engine: Any  # not checkpointed
```

**Nodes:**
- `run_engineer_node` — builds context (fresh/retry/revision), calls `run_agent()`, returns `{pr_url, branch_name, error}`
- `run_reviewer_node` — creates reviewer Task + Agent, calls `run_agent()` with provider/mr_info, returns `{review_verdict, review_feedback}`
- `run_revision_node` — injects review feedback as context, calls `run_agent()` on same branch, returns `{pr_url, error}`
- `retry_gate` — increments attempt counter, routes to engineer or failed
- `revision_gate` — increments revision counter, routes to revision or failed
- `mark_merged_node` — updates task status to MERGED, optionally calls `provider.merge_mr()` if auto_merge
- `mark_done_node` — updates task status to DONE (database_engineer path)
- `mark_failed_node` — updates task status to FAILED

**Conditional edges:**
- `route_after_engineer`: error → retry_gate; db_engineer → done; has_pr → review; no_pr → retry_gate
- `route_after_review`: approve → merged; request_changes → revision_gate
- `route_retry`: attempt < max → engineer; else → failed
- `route_revision`: count < max → revision; else → failed

**What it replaces:**
- `manage_dev_tasks()` — the 170-line imperative state machine
- `_try_complete_task()` — graph edge handles this
- `run_task_review()` — becomes `run_reviewer_node`
- Orphan detection for in-progress tasks — LangGraph node timeout
- `_has_running_agent()` guards — graph structure prevents double-launch

**What it keeps:**
- `run_engineer()` internals (context building, branch naming) — extracted into node helpers
- `get_review_feedback()` — called inside `run_revision_node`
- `build_checkpoint_summary()` — called inside `run_engineer_node` for retries
- DB writes inside each node (dual-write: graph state + DB)

**Testing approach:**
- Unit test each node in isolation with mock DB/engine
- Integration test the compiled graph with in-memory SQLite
- Test the revision loop: invoke graph, mock reviewer returning `request_changes` twice then `approve`
- Test max revisions: mock reviewer always returning `request_changes`, verify graph reaches `failed` after 3

**Migration:**
- Feature-flagged: `USE_LANGGRAPH_TASK=true` to opt in
- `manage_dev_tasks()` stays as fallback when flag is off
- `launch_engineers()` detects the flag and either spawns coroutines (old) or returns `Send()`s (new)

---

### Phase 2: Structured Fan-Out

**Goal:** Replace fire-and-forget `engine._spawn()` with LangGraph `Send()` for parallel task execution with checkpoint tracking.

**Changes to `engine/job_graph.py`:**

`engineer_dispatch_node` returns a list of `Send()`:
```python
async def engineer_dispatch_node(state: JobGraphState) -> list[Send]:
    engine = state["engine"]
    tasks = await engine.db.get_tasks(state["job_id"])
    engineer_roles = {"backend_engineer", "frontend_engineer", "database_engineer"}
    pending = [t for t in tasks if t.agent_role in engineer_roles and t.status == "pending"]

    return [
        Send("task_subgraph", {
            "job_id": state["job_id"],
            "task_id": t.id,
            "service": t.service,
            "agent_role": t.agent_role,
            "attempt": 1,
            "max_attempts": 3,
            "revision_count": 0,
            "max_revisions": 3,
            "pr_url": None,
            "branch_name": None,
            "review_verdict": None,
            "review_feedback": None,
            "error": None,
            "engine": engine,
        })
        for t in pending
    ]
```

**New node: `collect_task_results_node`** — join point after all Send branches complete:
```python
async def collect_task_results_node(state: JobGraphState) -> dict:
    engine = state["engine"]
    tasks = await engine.db.get_tasks(state["job_id"])
    engineer_roles = {"backend_engineer", "frontend_engineer", "database_engineer"}
    dev_tasks = [t for t in tasks if t.agent_role in engineer_roles]

    all_failed = all(t.status == "failed" for t in dev_tasks)
    has_merged = any(t.status in ("merged", "done") for t in dev_tasks)

    if all_failed:
        await engine.db.update_job_status(state["job_id"], JobStatus.FAILED, error="All dev tasks failed")
        return {"job_status": "failed", "error": "All dev tasks failed"}
    if has_merged:
        await engine.db.update_job_status(state["job_id"], JobStatus.MERGED)
        return {"job_status": "merged"}
    return {"job_status": "failed", "error": "No tasks merged"}
```

**Graph wiring:**
```python
graph.add_node("task_subgraph", build_task_graph())
graph.add_conditional_edges("engineer_dispatch", ...)  # Send() fan-out
graph.add_edge("task_subgraph", "collect_results")     # all branches → join
graph.add_conditional_edges("collect_results", route_after_collect)
```

**What it replaces:**
- `engine._spawn(run_engineer(...))` — replaced by `Send()`
- `_startup_cleanup()` orphan scanning — checkpoint resume handles it
- `busy_services` tracking — one `Send()` per service by construction
- Poll-loop latency for task transitions — graph edges route immediately

**What it keeps:**
- `_startup_cleanup()` as fallback for non-graph jobs (review jobs, legacy)
- Poll loop still calls `graph.ainvoke()` per active job (but the graph does all internal coordination)
- Arbiter heartbeat/anomaly detection (independent, still valuable)

**Checkpoint structure per job:**
```
job checkpoint (thread_id = job_id):
  ├── job-level state: {job_status, spec, ...}
  └── Send() branches:
       ├── task_subgraph (api):     {task_id, revision_count: 1, current_node: "revision_gate"}
       ├── task_subgraph (frontend): {task_id, current_node: "merged"}  (done)
       └── task_subgraph (database): {task_id, current_node: "done"}    (done)
```

On crash recovery: resume checkpoint → api task picks up at revision_gate, other two are already complete. No scanning.

---

### Phase 3: Cleanup (Optional)

After Phases 1-2 are stable and the feature flag is always-on:

- Delete `manage_dev_tasks()`
- Delete `_try_complete_task()`
- Delete `_has_running_agent()` guards
- Simplify `_startup_cleanup()` to only handle edge cases
- Remove `USE_LANGGRAPH_TASK` feature flag
- Delete the old `_advance()` if/elif dispatcher
- Remove `USE_LANGGRAPH_ENGINE` feature flag (graph is the only path)

---

## What Stays Unchanged

| Component | Reason |
|---|---|
| `state_transitions.py` | Defense-in-depth validation on all DB writes |
| `agents/runner.py` + `_agent_loop_generic()` | LiteLLM tool-use loop, called by graph nodes |
| `agents/graph.py` (agent subgraph) | Single-node wrapper, no benefit from more structure |
| `review_executor.py`, `mcp_executor.py` | Tool implementations, orthogonal to orchestration |
| `providers/git.py` | Git provider API, called by nodes |
| DB as source of truth | Graph state supplements but doesn't replace DB |
| Arbiter + NATS | Heartbeats, anomaly detection, circuit breaking — independent value |
| `server/mcp.py` | MCP tools for external integrations, unchanged |
| Review-only job graph | Simple enough that the current thin-router approach works fine |

---

## Risks and Mitigations

**Blocking agent nodes:** Agent execution takes minutes to hours. The graph node blocks for that duration.
→ Fine for async Python. Arbiter heartbeat/timeout detection runs independently and can still detect stuck agents.

**Dual writes (graph state + DB):** Each node writes to both graph state and DB.
→ DB write first, then return graph state. If the node crashes between them, checkpoint resume re-runs the node, and DB write is idempotent (status transition validation prevents duplicates).

**Checkpoint size:** One checkpoint per task subgraph per job. A job with 5 services = 5 task checkpoints.
→ Each checkpoint is small (< 1KB). Postgres/SQLite checkpointers handle this fine.

**Testing complexity:** Graph tests need LangGraph test infrastructure.
→ Nodes are testable as plain async functions. Graph integration tests use in-memory SQLite checkpointer.

**Rollback path:** Feature flags (`USE_LANGGRAPH_TASK`) let us disable the new path at any time. Old `manage_dev_tasks()` stays until Phase 3.

---

## Success Criteria

- [ ] Revision cycle visible as graph edges, not buried in conditionals
- [ ] Crash during revision cycle resumes from checkpoint (no orphan scan)
- [ ] Zero poll-loop latency between engineer finishing and reviewer launching
- [ ] Parallel tasks tracked in checkpoint (not fire-and-forget)
- [ ] `manage_dev_tasks()` deleted (Phase 3)
- [ ] All 372+ existing tests pass with flag on and off
