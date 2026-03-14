## ADDED Requirements

### Requirement: TupleSpace out operation
The TupleSpace SHALL provide `async out(category, key, value, tags, agent_role, job_id, ttl) -> str` that publishes a fact to the L2 cache, returning a unique fact ID. Facts SHALL be scoped to the TupleSpace's project.

#### Scenario: Publish a fact
- **WHEN** calling `tuplespace.out(category="decision", key="db-choice", value="PostgreSQL", tags=["infra"])`
- **THEN** a Fact is stored in the backend with a unique ID and the TupleSpace's project scope
- **THEN** the fact is retrievable via `rd`

#### Scenario: Publish with TTL
- **WHEN** calling `tuplespace.out(category="temp", key="lock", value="held", ttl=60)`
- **THEN** the fact expires from the backend after 60 seconds

### Requirement: TupleSpace rd operation
The TupleSpace SHALL provide `async rd(category, key_pattern, tags, limit) -> list[Fact]` that queries facts non-destructively. Results SHALL be filtered by project scope.

#### Scenario: Query by category
- **WHEN** two facts exist with category="decision" in project "payments-api"
- **WHEN** calling `tuplespace.rd(category="decision")`
- **THEN** both facts are returned

#### Scenario: Query with tag filter
- **WHEN** facts exist with tags=["auth"] and tags=["infra"]
- **WHEN** calling `tuplespace.rd(category="decision", tags=["auth"])`
- **THEN** only the fact tagged "auth" is returned

#### Scenario: Query returns empty for other projects
- **WHEN** a fact exists in project "payments-api"
- **WHEN** querying from a TupleSpace scoped to project "other-project"
- **THEN** zero results are returned

### Requirement: TupleSpace in_ operation
The TupleSpace SHALL provide `async in_(category, key_pattern) -> Fact | None` that atomically reads and deletes a matching fact (Linda consume). Returns None if no match.

#### Scenario: Atomic consume
- **WHEN** a fact exists with category="task" and key="pending-review"
- **WHEN** two agents call `in_(category="task", key_pattern="pending-review")` concurrently
- **THEN** exactly one agent receives the fact and the other receives None
- **THEN** the fact is deleted from the backend

### Requirement: TupleSpace count operation
The TupleSpace SHALL provide `async count(category) -> int` returning the number of facts in the given category for the current project.

#### Scenario: Count facts
- **WHEN** 3 facts exist with category="decision" in the current project
- **WHEN** calling `tuplespace.count("decision")`
- **THEN** the result is 3

### Requirement: TupleSpace expire_project operation
The TupleSpace SHALL provide `async expire_project() -> int` that removes all facts for the current project, returning the count of removed facts.

#### Scenario: Expire all project facts
- **WHEN** 5 facts exist in the current project
- **WHEN** calling `tuplespace.expire_project()`
- **THEN** all 5 facts are removed and the return value is 5

### Requirement: Redis TupleSpace backend
The `RedisTupleSpaceBackend` SHALL implement `TupleSpaceBackend` using `redis.asyncio` with RedisJSON for storage and RediSearch for indexing/querying.

#### Scenario: Index creation on connect
- **WHEN** calling `backend.connect()`
- **THEN** a RediSearch index is created with fields: project (TAG), category (TAG), key (TAG), value (TEXT), tags (TAG), timestamp (NUMERIC SORTABLE)

#### Scenario: Atomic pop via Lua script
- **WHEN** calling `backend.atomic_pop(index, query)`
- **THEN** the operation uses a Lua script to atomically search, retrieve, and delete in a single Redis round-trip

### Requirement: MCP memory tools
The MCP server SHALL expose 3 memory tools when `memory_enabled` is True: `publish_fact`, `query_facts`, `create_memory_note`. These tools SHALL NOT appear when `memory_enabled` is False.

#### Scenario: Tools gated by feature flag
- **WHEN** `memory_enabled` is False
- **THEN** `publish_fact`, `query_facts`, and `create_memory_note` are not registered on the MCP server

#### Scenario: publish_fact tool
- **WHEN** an agent calls `publish_fact(project, category, key, value, tags, job_id, agent_role)`
- **THEN** the tool delegates to `tuplespace.out()` and returns the fact ID

#### Scenario: query_facts tool
- **WHEN** an agent calls `query_facts(project, category, key_pattern, tags, limit)`
- **THEN** the tool delegates to `tuplespace.rd()` and returns matching facts

### Requirement: Agent tool definitions for memory
The tool definitions module SHALL include `_MEMORY_TOOLS` schemas. `get_tools_for_role()` SHALL accept a `memory_enabled` flag and append memory tools when True.

#### Scenario: Memory tools in tool list
- **WHEN** calling `get_tools_for_role("CODE_REVIEWER", memory_enabled=True)`
- **THEN** the returned tool list includes publish_fact, query_facts, and create_memory_note schemas

#### Scenario: No memory tools when disabled
- **WHEN** calling `get_tools_for_role("CODE_REVIEWER", memory_enabled=False)`
- **THEN** the returned tool list does NOT include any memory tool schemas
