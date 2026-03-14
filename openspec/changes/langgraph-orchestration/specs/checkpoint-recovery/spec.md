## ADDED Requirements

### Requirement: Checkpointer backend selection
The system SHALL use `AsyncPostgresSaver` when `DB_BACKEND=postgres` and `AsyncSqliteSaver` when using SQLite, matching the existing database backend configuration.

#### Scenario: PostgreSQL backend
- **WHEN** `DB_BACKEND` is `postgres` and `POSTGRES_URL` is set
- **THEN** the system SHALL initialize `AsyncPostgresSaver` using the same connection parameters and pass it to `graph.compile(checkpointer=...)`

#### Scenario: SQLite backend
- **WHEN** `DB_BACKEND` is not `postgres`
- **THEN** the system SHALL initialize `AsyncSqliteSaver` using the `DB_PATH` configuration and pass it to `graph.compile(checkpointer=...)`

### Requirement: Job graph checkpointing
The system SHALL checkpoint the job graph state after each node completes, using the `job_id` as the LangGraph `thread_id`.

#### Scenario: State persisted after node completion
- **WHEN** a graph node (e.g., `spec_analysis`, `engineer_dispatch`) completes successfully
- **THEN** the checkpointer SHALL persist the updated `JobGraphState` to the checkpoint store

#### Scenario: Thread ID matches job ID
- **WHEN** the job graph is invoked
- **THEN** the config SHALL include `{"configurable": {"thread_id": job.id}}`

### Requirement: Fault-tolerant resume after crash
The system SHALL detect incomplete job graphs on startup and resume them from the last checkpoint rather than restarting from scratch.

#### Scenario: Engine restart with in-progress job
- **WHEN** the engine starts and finds a job with status `DEV_IN_PROGRESS` that has a checkpoint
- **THEN** the system SHALL resume the job graph from the last checkpointed state by calling `app.ainvoke(None, config)` with the job's thread_id

#### Scenario: Engine restart without checkpoint
- **WHEN** the engine starts and finds a job with status `DEV_IN_PROGRESS` but no checkpoint exists
- **THEN** the system SHALL fall back to the existing `_startup_cleanup()` recovery logic (mark orphaned agents failed, reset tasks to PENDING)

### Requirement: Checkpoint table isolation
The system SHALL configure the LangGraph checkpointer to use a table prefix or separate schema to avoid conflicts with the application's existing tables.

#### Scenario: No table name conflicts
- **WHEN** the checkpointer creates its storage tables
- **THEN** the table names SHALL be prefixed with `langgraph_` (e.g., `langgraph_checkpoints`, `langgraph_writes`) to avoid conflicts with existing application tables

### Requirement: Checkpoint cleanup
The system SHALL delete checkpoints for completed or failed jobs to prevent unbounded storage growth.

#### Scenario: Job completes successfully
- **WHEN** a job transitions to `DONE` or `DEPLOYED` status
- **THEN** the system SHALL delete all checkpoints for that job's thread_id

#### Scenario: Job fails terminally
- **WHEN** a job transitions to `FAILED` with no remaining retry path
- **THEN** the system SHALL delete all checkpoints for that job's thread_id

### Requirement: Retry policy for transient failures
The system SHALL configure LangGraph `RetryPolicy` on graph nodes that make external calls (LLM, git provider, K8s), with exponential backoff matching the current retry behavior.

#### Scenario: LLM rate limit during orchestration
- **WHEN** a graph node encounters a rate limit error from the LLM provider
- **THEN** the retry policy SHALL retry up to 5 times with exponential backoff (base 5s, max 60s), matching the current `_agent_loop_generic` retry behavior

#### Scenario: Non-retryable error
- **WHEN** a graph node encounters a non-transient error (e.g., invalid state transition)
- **THEN** the retry policy SHALL NOT retry and the error SHALL propagate to the graph's error handling
