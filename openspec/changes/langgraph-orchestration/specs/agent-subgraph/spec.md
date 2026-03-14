## ADDED Requirements

### Requirement: Agent subgraph wrapper
The system SHALL define a reusable LangGraph subgraph that wraps `_agent_loop_generic()` as a single execution node, preserving the existing LiteLLM tool-use loop, wind-down mechanism, and cost tracking.

#### Scenario: Engineer agent runs as subgraph
- **WHEN** the orchestration graph dispatches an engineer task
- **THEN** the agent subgraph SHALL call `_agent_loop_generic()` with the same parameters as the current `run_agent()` function and return the result dict (tokens, cost, turns, status)

#### Scenario: Reviewer agent runs as subgraph
- **WHEN** the orchestration graph dispatches a code review task
- **THEN** the agent subgraph SHALL call `_agent_loop_generic()` with review-specific tools, executor, and prompt, and return verdict/summary/comments_posted in addition to standard metrics

### Requirement: Agent subgraph state schema
The system SHALL define an `AgentSubgraphState` TypedDict containing `job_id`, `task_id`, `agent_id`, `agent_role`, `model`, `system_prompt`, `tools`, `tool_executor`, `timeout`, `max_turns`, and `result`. The result field SHALL contain the output dict from `_agent_loop_generic()`.

#### Scenario: State initialized from task
- **WHEN** the parent graph sends state to the agent subgraph
- **THEN** the subgraph SHALL construct the system prompt, tools, and tool executor based on `agent_role` using the existing `build_agent_prompt()` / `build_prompt()` and `get_tools_for_role()` functions

### Requirement: Wind-down mechanism preserved
The system SHALL preserve the existing three-phase wind-down mechanism (normal → soft warning at 80% → hard tool-blocking at 90%) inside the agent subgraph's inner loop. LangGraph's `recursion_limit` SHALL NOT be used as a substitute.

#### Scenario: Wind-down at 80% turns
- **WHEN** the agent reaches 80% of max_turns or 80% of timeout
- **THEN** the inner loop SHALL inject a wind-down warning message, identical to current behavior

#### Scenario: Hard-stop at 90% turns
- **WHEN** the agent reaches 90% of max_turns or 90% of timeout
- **THEN** the inner loop SHALL block all tool calls except wrap-up tools (`create_branch`, `commit`, `push`, `create_pr`, `report_pr`, `complete_subtask`, `fail_subtask`, `update_task_status`, `send_heartbeat`, `submit_review`, `report_review_complete`)

### Requirement: Agent DB record lifecycle
The system SHALL create and update `Agent` DB records at the same lifecycle points as the current implementation: creation before execution, status update to `running` at start, and final update with tokens/cost/turns/status on completion or failure.

#### Scenario: Agent record created before execution
- **WHEN** the agent subgraph begins execution
- **THEN** it SHALL create an `Agent` record in the DB with status `created` and update to `running` before the first LLM call

#### Scenario: Agent record updated on completion
- **WHEN** the inner loop completes (success or failure)
- **THEN** the subgraph SHALL update the `Agent` record with `input_tokens`, `output_tokens`, `cost_usd`, `num_turns`, `status`, `finished_at`, and `error` (if any)

### Requirement: Tool call recording preserved
The system SHALL continue recording tool calls to the `tool_calls` DB table during agent execution, with `tool_name`, `params`, `result`, `error`, `duration_ms`, and `job_id`.

#### Scenario: Tool call recorded to DB
- **WHEN** an agent executes a tool call during the inner loop
- **THEN** the system SHALL call `db.record_tool_call()` with timing and result data, identical to current behavior
