## 1. Dependencies & Configuration

- [x] 1.1 Add `langgraph>=1.1`, `langchain-core>=0.3`, `langchain-community>=0.3`, `langgraph-checkpoint-postgres`, `langgraph-checkpoint-sqlite` to `pyproject.toml`
- [x] 1.2 Run `uv lock` and verify dependency resolution with existing deps (especially `litellm`, `pydantic`)
- [x] 1.3 Add `USE_LANGGRAPH_AGENT` and `USE_LANGGRAPH_ENGINE` feature flags to `Config` dataclass in `config.py` (default `false`)
- [x] 1.4 Add checkpointer factory function in a new `minions/engine/checkpointer.py` that returns `AsyncPostgresSaver` or `AsyncSqliteSaver` based on `DB_BACKEND`, with `langgraph_` table prefix

## 2. Agent Subgraph Wrapper (Phase 1)

- [x] 2.1 Define `AgentSubgraphState(TypedDict)` in a new `minions/agents/graph.py` with fields: `job_id`, `task_id`, `agent_id`, `agent_role`, `model`, `system_prompt`, `tools`, `tool_executor`, `timeout`, `max_turns`, `result`, `error`
- [x] 2.2 Implement `agent_execution_node()` — async function that calls `_agent_loop_generic()` with state params, writes result to state, and updates Agent DB record (create/running/done/failed lifecycle)
- [x] 2.3 Implement `build_agent_subgraph()` — returns a compiled `StateGraph(AgentSubgraphState)` with single node `execute` and `RetryPolicy` for rate limit errors (5 retries, exponential backoff base 5s, max 60s)
- [x] 2.4 Update `run_agent()` to optionally use the subgraph when `USE_LANGGRAPH_AGENT=true`, falling back to direct `_agent_loop_generic()` call when false
- [x] 2.5 Write unit tests for `build_agent_subgraph()` — verify state initialization, result propagation, retry on rate limit, DB record lifecycle (mock DB + LiteLLM)

## 3. Job Orchestration Graph (Phase 2)

- [x] 3.1 Define `JobGraphState(TypedDict)` in a new `minions/engine/job_graph.py` with fields: `job_id`, `job_status`, `tasks`, `active_agents`, `current_phase`, `error`
- [x] 3.2 Implement `spec_analysis_node()` — wraps `launch_spec_analyst()` logic, reads/writes DB, returns updated state
- [x] 3.3 Implement `task_decomposition_node()` — wraps `launch_arbiter()` logic, reads/writes DB, returns updated state
- [x] 3.4 Implement `engineer_dispatch_node()` — uses `Send()` to fan out agent subgraphs per pending service, respecting busy-service constraint (one task per service)
- [x] 3.5 Implement `review_node()` — wraps `run_task_review()` logic, returns verdict in state
- [x] 3.6 Implement `merge_node()` — handles auto-merge or transition to MERGED status
- [x] 3.7 Implement `deploy_node()` — wraps `launch_deploy_monitor()` logic
- [x] 3.8 Implement `completion_node()` — transitions job to DONE, uploads artifacts
- [x] 3.9 Implement `fail_node()` — transitions job to FAILED with error context
- [x] 3.10 Define conditional edge functions: `route_after_spec()`, `route_after_arbiter()`, `route_after_engineer()`, `route_after_review()` (approve → merge, changes_requested → revision, max_revisions → fail)
- [x] 3.11 Assemble `build_job_graph()` — connects all nodes and edges, attaches checkpointer, returns compiled graph
- [x] 3.12 Write unit tests for each node function in isolation (mock DB, verify state transitions)
- [x] 3.13 Write integration test for full graph execution with in-memory checkpointer and mock agents

## 4. Review Cycle Graph (Phase 3)

- [x] 4.1 Implement `engineer_revision_node()` — wraps `run_engineer(is_revision=True)`, injects review feedback from Messages table
- [x] 4.2 Implement `pr_open_node()` — transitions task to PR_OPEN, triggers review
- [x] 4.3 Wire review cycle as graph cycle: `engineer_dispatch` → `pr_open` → `review` → conditional → (`merge` | `engineer_revision` → `pr_open`)
- [x] 4.4 Add max-revision guard in `route_after_review()` — if `revision_count >= max_attempts`, route to `fail_node` instead of revision
- [x] 4.5 Write unit test for review cycle: approve path exits, changes_requested loops, max revisions fails

## 5. Checkpoint Recovery (Phase 3)

- [x] 5.1 Implement `resume_from_checkpoint()` in `job_graph.py` — given a job_id, check if checkpoint exists, resume graph if so
- [x] 5.2 Update `JobEngine._startup_cleanup()` to call `resume_from_checkpoint()` for in-progress jobs when `USE_LANGGRAPH_ENGINE=true`, falling back to existing orphan recovery when no checkpoint found
- [x] 5.3 Implement checkpoint cleanup in `completion_node()` and `fail_node()` — delete checkpoints for completed/failed jobs
- [x] 5.4 Write test for crash recovery: checkpoint after `engineer_dispatch`, simulate restart, verify resume from checkpoint

## 6. Engine Integration & Feature Flags

- [x] 6.1 Update `JobEngine._advance()` to dispatch via job graph when `USE_LANGGRAPH_ENGINE=true`, falling back to existing if/elif when false
- [x] 6.2 Update `_run_in_process()` to use agent subgraph when `USE_LANGGRAPH_AGENT=true`
- [x] 6.3 Ensure K8s dispatch path works as async node — `_dispatch_k8s()` wrapped in graph node that publishes to NATS and awaits result
- [x] 6.4 Verify NATS result handler (`_on_nats_result`) still works when graph is active — results update DB, graph node polls DB for completion
- [x] 6.5 Run full test suite (`uv run pytest`) — all 372+ existing tests must pass with feature flags disabled

## 7. Review Job Graph

- [x] 7.1 Implement simplified `build_review_job_graph()` — `review_dispatch` → `review_completion` (no engineer/revision cycle)
- [x] 7.2 Update `review.py` handlers to use review job graph when `USE_LANGGRAPH_ENGINE=true`
- [x] 7.3 Write test for review job graph: single reviewer task, verdict captured, job transitions to DONE

## 8. Cleanup & Documentation

- [x] 8.1 Update `.env.example` with `USE_LANGGRAPH_AGENT` and `USE_LANGGRAPH_ENGINE` variables
- [x] 8.2 Update `CLAUDE.md` architecture section with LangGraph graph descriptions and new file locations
- [x] 8.3 Run `ruff format` and `ruff check --fix` on all new/modified files
- [x] 8.4 Verify Docker build succeeds with new dependencies (`task docker:build`)
