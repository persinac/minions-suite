## ADDED Requirements

### Requirement: Job orchestration graph definition
The system SHALL define a LangGraph `StateGraph` that models the dev job lifecycle as explicit nodes and conditional edges, replacing the `_advance()` dispatcher in `job_engine.py`.

#### Scenario: Dev job executes through all phases
- **WHEN** a dev job is created with status `SPEC_RECEIVED`
- **THEN** the graph SHALL execute nodes in order: `spec_analysis` → `task_decomposition` → `engineer_dispatch` → `review_cycle` → `deploy` → `completion`, with conditional edges routing based on job and task status

#### Scenario: Review job uses simplified graph
- **WHEN** a review job is created with status `TASKS_CREATED`
- **THEN** the graph SHALL execute: `review_dispatch` → `review_completion`, skipping dev-specific nodes

### Requirement: Job graph state schema
The system SHALL define a `JobGraphState` TypedDict that contains `job_id`, `job_status`, `tasks` (list of task snapshots), `active_agents`, `current_phase`, and `error`. The DB SHALL remain the source of truth — graph state is a routing view.

#### Scenario: State reflects DB on each node entry
- **WHEN** a graph node begins execution
- **THEN** the node SHALL read current `Job` and `Task` records from the DB to populate state, not rely on stale graph state from a prior node

### Requirement: Service-level parallel dispatch
The system SHALL use LangGraph's `Send()` API to fan out engineer subgraph executions across services in parallel, maintaining the current constraint that only one task per service runs at a time.

#### Scenario: Multiple services execute in parallel
- **WHEN** the arbiter creates tasks for services `backend` and `frontend`
- **THEN** the `engineer_dispatch` node SHALL emit `Send("engineer_subgraph", ...)` for both services concurrently

#### Scenario: Same-service tasks are serialized
- **WHEN** a service has a task in `IN_PROGRESS`, `PR_OPEN`, or `IN_REVIEW` status
- **THEN** the `engineer_dispatch` node SHALL NOT emit a `Send()` for another task on that service

### Requirement: Review cycle as graph cycle
The system SHALL model the engineer → reviewer → revision loop as an explicit graph cycle with conditional edges, replacing the status-polling logic in `manage_dev_tasks()`.

#### Scenario: Approved review exits cycle
- **WHEN** the reviewer node produces verdict `approve`
- **THEN** the conditional edge SHALL route to the `merge` node, exiting the cycle

#### Scenario: Changes requested triggers revision
- **WHEN** the reviewer node produces verdict `changes_requested`
- **THEN** the conditional edge SHALL route back to the `engineer_revision` node, incrementing `revision_count` on the task

#### Scenario: Max revisions exhausted
- **WHEN** `revision_count` reaches `max_attempts` and the reviewer still requests changes
- **THEN** the conditional edge SHALL route to a `fail_task` node instead of another revision

### Requirement: Feature flag for gradual rollout
The system SHALL support a `USE_LANGGRAPH_ENGINE` environment variable. When `false`, the existing `_advance()` dispatcher SHALL be used. When `true`, the LangGraph job graph SHALL be used.

#### Scenario: Feature flag disabled
- **WHEN** `USE_LANGGRAPH_ENGINE` is unset or `false`
- **THEN** `JobEngine._advance()` SHALL use the existing if/elif dispatcher

#### Scenario: Feature flag enabled
- **WHEN** `USE_LANGGRAPH_ENGINE` is `true`
- **THEN** `JobEngine._advance()` SHALL invoke the compiled LangGraph job graph

### Requirement: K8s dispatch compatibility
The system SHALL support K8s agent dispatch as an async graph node that publishes work items to NATS and awaits results, preserving the existing `_dispatch_k8s()` behavior.

#### Scenario: K8s-enabled agent dispatch
- **WHEN** `config.k8s_enabled` is true and an engineer task is dispatched
- **THEN** the engineer subgraph node SHALL call `_dispatch_k8s()` and await the NATS result message before returning node output
