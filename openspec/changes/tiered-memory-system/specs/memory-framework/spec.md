## ADDED Requirements

### Requirement: Package structure
The `agent-memory` package SHALL exist as a standalone Python package at `agent-memory/` in the monorepo root with its own `pyproject.toml` requiring Python >= 3.14.

#### Scenario: Package installable
- **WHEN** running `uv pip install -e agent-memory/`
- **THEN** the `agent_memory` Python package is importable

#### Scenario: No framework coupling
- **WHEN** importing `agent_memory`
- **THEN** the only required dependency is `pydantic>=2.0`; backend-specific deps (redis, psycopg, litellm) are optional extras

### Requirement: MemoryNode data model
The system SHALL define a `MemoryNode` Pydantic model with fields: `id` (str), `content` (str), `title` (str | None), `tags` (list[str]), `created_at` (str), `embedding` (list[float] | None), `attributes` (dict), `source_job_id` (str | None), `source_agent_role` (str | None), `project` (str), `access_count` (int), `links` (list[str]).

#### Scenario: Create a MemoryNode
- **WHEN** constructing a MemoryNode with `content="auth uses JWT"`, `project="payments-api"`, `tags=["auth", "pattern"]`
- **THEN** the model validates successfully with defaults for optional fields (access_count=0, links=[], embedding=None)

### Requirement: Fact data model
The system SHALL define a `Fact` Pydantic model with fields: `category` (str), `key` (str), `value` (str), `tags` (list[str]), `agent_role` (str | None), `job_id` (str | None), `project` (str), `timestamp` (float).

#### Scenario: Create a Fact
- **WHEN** constructing a Fact with `category="decision"`, `key="db-choice"`, `value="PostgreSQL"`, `project="payments-api"`, `timestamp=1710000000.0`
- **THEN** the model validates successfully

### Requirement: Entity data model
The system SHALL define an `Entity` Pydantic model with fields: `id` (str), `name` (str), `entity_type` (str | None), `project` (str).

#### Scenario: Create an Entity
- **WHEN** constructing an Entity with `name="auth-module"`, `project="payments-api"`
- **THEN** the model validates successfully with `entity_type=None`

### Requirement: TupleSpaceBackend protocol
The system SHALL define a `@runtime_checkable` Protocol `TupleSpaceBackend` with async methods: `connect()`, `close()`, `put(key, doc, ttl)`, `get(key)`, `delete(key)`, `search(index, query, limit)`, `atomic_pop(index, query)`, `keys(pattern)`, `create_index(name, schema)`.

#### Scenario: Protocol checking
- **WHEN** a class implements all TupleSpaceBackend methods
- **THEN** `isinstance(instance, TupleSpaceBackend)` returns True

### Requirement: MemoryStoreBackend protocol
The system SHALL define a `@runtime_checkable` Protocol `MemoryStoreBackend` with async methods: `connect()`, `close()`, `create_node(node)`, `get_node(node_id)`, `query_by_tags(project, tags, limit)`, `query_by_similarity(project, embedding, limit)`, `create_link(from_id, to_entity, link_type, confidence, reasoning)`, `get_backlinks(entity_name, project, limit)`, `ensure_entity(name, project, entity_type)`, `increment_access(node_id)`.

#### Scenario: Protocol checking
- **WHEN** a class implements all MemoryStoreBackend methods
- **THEN** `isinstance(instance, MemoryStoreBackend)` returns True

### Requirement: EmbeddingProvider protocol
The system SHALL define a `@runtime_checkable` Protocol `EmbeddingProvider` with async method `embed(text) -> list[float]` and property `dimensions -> int`.

#### Scenario: LiteLLM embedding provider
- **WHEN** creating a `LiteLLMEmbeddingProvider` with model `text-embedding-3-small`
- **THEN** it implements `EmbeddingProvider` and `dimensions` returns 1536

### Requirement: Tag normalization
The system SHALL provide `normalize_tags(raw: list[str]) -> list[str]` that lowercases, deduplicates, strips whitespace, and validates against a controlled vocabulary. `suggest_extensions(raw: list[str]) -> list[str]` SHALL suggest related tags from the vocabulary.

#### Scenario: Normalize tags
- **WHEN** calling `normalize_tags(["Auth", " auth", "PATTERN", "unknown-tag"])`
- **THEN** the result is `["auth", "pattern", "unknown-tag"]` (lowered, deduped, whitespace stripped)

#### Scenario: Suggest extensions
- **WHEN** calling `suggest_extensions(["auth"])` and the vocabulary maps auth to security-related tags
- **THEN** the result includes related tags like `["security", "authentication"]`

### Requirement: Public API surface
The `agent_memory` package `__init__.py` SHALL re-export: `TupleSpace`, `MemoryStore`, `MemoryNode`, `Fact`, `Entity`, `TupleSpaceBackend`, `MemoryStoreBackend`, `EmbeddingProvider`, `get_relevant_memories`, `get_file_backlinks`, `build_knowledge_context`, `build_file_context`, `normalize_tags`.

#### Scenario: Import public API
- **WHEN** running `from agent_memory import TupleSpace, MemoryStore, MemoryNode`
- **THEN** all imports succeed without errors
