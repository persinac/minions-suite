# Tiered Memory System — Implementation Plan

## Context

Minion Suite agents are stateless between invocations. We're building a **modular, framework-grade tiered memory system** (`agent-memory`) as a standalone package in the monorepo, then integrating it into Minion Suite. The framework is backend-pluggable via Protocol classes so any agent system can use it.

Design decisions are documented in `notes/distributed-memory-knowledge-graphs.md`.

## Additional notes

- Package naming: agent-memory as the pip package, agent_memory as the Python package. Check PyPI for name conflicts.
- AGE Docker image: apache/age:PG17_latest bundles AGE but not pgvector. May need a custom Dockerfile that layers pgvector on top, or use pgvector/pgvector:pg17 as base and
install AGE.
- fakeredis for tests: Confirm fakeredis[json] supports RediSearch FT.SEARCH commands. If not, tests may need a thin mock layer over the search operations.


---

## Package Structure

```
minions-suite/                        # existing monorepo root
├── minions/                          # existing app (consumer)
│   └── pyproject.toml                # adds agent-memory as path dependency
├── agent-memory/                     # NEW — standalone framework package
│   ├── pyproject.toml
│   ├── agent_memory/
│   │   ├── __init__.py               # public API re-exports
│   │   ├── types.py                  # shared data models (MemoryNode, Fact, Entity)
│   │   ├── tuplespace.py             # TupleSpace (L2 — Linda primitives)
│   │   ├── store.py                  # MemoryStore (L3 — knowledge graph CRUD)
│   │   ├── retrieval.py              # MAGMA-style retrieval + scoring
│   │   ├── context.py                # Prompt context builders (Obsidian-style)
│   │   ├── archiver.py               # L2→L3 archival (fast + slow paths)
│   │   ├── tags.py                   # Controlled vocabulary + normalization
│   │   ├── embeddings.py             # Vendor-agnostic embedding helper
│   │   ├── decay.py                  # Access-frequency decay
│   │   ├── protocols.py              # Backend protocol definitions
│   │   └── backends/
│   │       ├── __init__.py
│   │       ├── redis.py              # RedisTupleSpaceBackend (L2)
│   │       └── postgres.py           # PostgresMemoryBackend (L3 — AGE + pgvector)
│   └── tests/
│       ├── conftest.py
│       ├── test_tuplespace.py
│       ├── test_store.py
│       ├── test_retrieval.py
│       ├── test_context.py
│       ├── test_tags.py
│       └── test_archiver.py
├── docker-compose.yml                # add redis service, swap postgres image
└── ...
```

---

## Phase 1: Framework Skeleton + Infrastructure

**Goal:** Create the `agent-memory` package, define all Protocol interfaces, set up Redis + AGE/pgvector in Docker, wire config into Minion Suite.

### New files — `agent-memory/`

**`agent-memory/pyproject.toml`**
- Package name: `agent-memory`
- Python >=3.14
- Core deps: `pydantic>=2.0` (models only — no framework coupling)
- Optional deps: `redis[hiredis]>=5.0` (L2 backend), `psycopg[binary,pool]>=3.1` (L3 backend), `litellm>=1.50.0` (embeddings)
- Test deps: `pytest>=8.0`, `pytest-asyncio>=0.24`, `fakeredis>=2.0`

**`agent_memory/protocols.py`** — All backend interfaces as `@runtime_checkable` Protocols:

```python
@runtime_checkable
class TupleSpaceBackend(Protocol):
    """Backend for L2 shared cache (e.g., Redis, NATS KV)."""
    async def connect(self) -> None: ...
    async def close(self) -> None: ...
    async def put(self, key: str, doc: dict, ttl: int | None = None) -> None: ...
    async def get(self, key: str) -> dict | None: ...
    async def delete(self, key: str) -> bool: ...
    async def search(self, index: str, query: str, limit: int = 20) -> list[dict]: ...
    async def atomic_pop(self, index: str, query: str) -> dict | None: ...
    async def keys(self, pattern: str) -> list[str]: ...
    async def create_index(self, name: str, schema: dict) -> None: ...

@runtime_checkable
class MemoryStoreBackend(Protocol):
    """Backend for L3 knowledge graph (e.g., Postgres+AGE+pgvector)."""
    async def connect(self) -> None: ...
    async def close(self) -> None: ...
    async def create_node(self, node: MemoryNode) -> str: ...
    async def get_node(self, node_id: str) -> MemoryNode | None: ...
    async def query_by_tags(self, project: str, tags: list[str], limit: int = 20) -> list[MemoryNode]: ...
    async def query_by_similarity(self, project: str, embedding: list[float], limit: int = 10) -> list[MemoryNode]: ...
    async def create_link(self, from_id: str, to_entity: str, link_type: str, confidence: float = 1.0, reasoning: str | None = None) -> None: ...
    async def get_backlinks(self, entity_name: str, project: str, limit: int = 20) -> list[MemoryNode]: ...
    async def ensure_entity(self, name: str, project: str, entity_type: str | None = None) -> str: ...
    async def increment_access(self, node_id: str) -> None: ...

@runtime_checkable
class EmbeddingProvider(Protocol):
    """Embedding generation (e.g., LiteLLM, OpenAI, local model)."""
    async def embed(self, text: str) -> list[float]: ...
    @property
    def dimensions(self) -> int: ...
```

**`agent_memory/types.py`** — Pydantic data models (zero framework deps):

```python
class MemoryNode(BaseModel):
    id: str
    content: str
    title: str | None = None
    tags: list[str] = []
    created_at: str
    embedding: list[float] | None = None
    attributes: dict = {}
    source_job_id: str | None = None
    source_agent_role: str | None = None
    project: str
    access_count: int = 0
    links: list[str] = []       # entity names this note links to

class Fact(BaseModel):
    category: str
    key: str
    value: str
    tags: list[str] = []
    agent_role: str | None = None
    job_id: str | None = None
    project: str
    timestamp: float

class Entity(BaseModel):
    id: str
    name: str
    entity_type: str | None = None
    project: str
```

**`agent_memory/tuplespace.py`** — Framework-level TupleSpace class (delegates to backend):
- `__init__(self, backend: TupleSpaceBackend, project: str)`
- `async def out(category, key, value, tags, agent_role, job_id, ttl) -> str`
- `async def rd(category, key_pattern, tags, limit) -> list[Fact]`
- `async def in_(category, key_pattern) -> Fact | None`
- `async def watch(pattern, callback)` — reactive subscription
- `async def count(category) -> int`
- `async def expire_project() -> int`

**`agent_memory/store.py`** — Framework-level MemoryStore class (delegates to backend):
- `__init__(self, backend: MemoryStoreBackend)`
- All CRUD operations delegating to backend
- `query_by_tags`, `query_by_similarity`, `get_backlinks`, `ensure_entity`, `increment_access`

**`agent_memory/tags.py`** — Controlled vocabulary + normalization:
- `CONTROLLED_TAGS` — domain, action, outcome tag sets
- `normalize_tags(raw) -> list[str]`
- `suggest_extensions(raw) -> list[str]`

**`agent_memory/embeddings.py`** — Default LiteLLM embedding provider:
- `class LiteLLMEmbeddingProvider` implementing `EmbeddingProvider`
- `async def embed(text) -> list[float]` via `litellm.aembedding()`

**`agent_memory/__init__.py`** — Public API:
```python
from .tuplespace import TupleSpace
from .store import MemoryStore
from .types import MemoryNode, Fact, Entity
from .protocols import TupleSpaceBackend, MemoryStoreBackend, EmbeddingProvider
from .retrieval import get_relevant_memories, get_file_backlinks
from .context import build_knowledge_context, build_file_context
from .tags import normalize_tags
```

### New files — backends (ship with framework)

**`agent_memory/backends/redis.py`** — `RedisTupleSpaceBackend` implementing `TupleSpaceBackend`:
- Uses `redis.asyncio` + RedisJSON + RediSearch
- `connect()` — creates async Redis connection, calls `create_index()`
- `put()` — `JSON.SET` + `EXPIRE`
- `search()` — `FT.SEARCH` with RediSearch query syntax
- `atomic_pop()` — Lua script: search + get + delete atomically
- Index schema for facts: project (TAG), category (TAG), key (TAG), value (TEXT), tags (TAG), timestamp (NUMERIC SORTABLE)

**`agent_memory/backends/postgres.py`** — `PostgresMemoryBackend` implementing `MemoryStoreBackend`:
- Uses `psycopg3` async pool (accepts pool or connection string)
- `create_node()` — INSERT into memory_nodes with pgvector embedding
- `query_by_similarity()` — `ORDER BY embedding <=> $1 LIMIT $2`
- `get_backlinks()` — JOIN memory_links on to_entity
- `ensure_entity()` — INSERT ON CONFLICT DO NOTHING + SELECT
- Graph traversal queries (Cypher via AGE when available, fallback to recursive CTEs)

### Infrastructure changes

**`docker-compose.yml`** — Add redis service, swap postgres image:
- Add `redis: image: redis/redis-stack:latest` with healthcheck, port 6379, volume `redisdata`
- Change `postgres: image: postgres:17` → `image: apache/age:PG17_latest`
- Add `redis` to `minion-suite.depends_on`

**`database/pgsql/migrations/20260314120000_add_memory_extensions.sql`**:
```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS age;
LOAD 'age';
SELECT ag_catalog.create_graph('knowledge');
```

**`database/pgsql/migrations/20260314130000_create_memory_tables.sql`**:
- `minions.memory_nodes` — id, content, title, tags TEXT[], embedding vector(1536), attributes JSONB, source_job_id, source_agent_role, project, access_count, last_accessed, created_at
- `minions.memory_links` — from_node, to_entity, link_type, confidence, reasoning, created_at
- `minions.memory_entities` — id, name, entity_type, project, first_seen, attributes
- GIN index on tags, ivfflat index on embedding, btree indexes on project + created_at

**`minions/pyproject.toml`** — Add: `agent-memory = {path = "../agent-memory"}`

**`minions/config.py`** — Add fields:
- `memory_enabled: bool = False`
- `redis_url: str = "redis://localhost:6379"`
- `redis_password: str = ""`
- `memory_l3_token_budget: int = 2000`

**`.env.example`** — Add `MEMORY_ENABLED`, `REDIS_URL`, `REDIS_PASSWORD`

**`minions/preflight.py`** — Add `check_redis()` when `memory_enabled`

### Tests

**`agent-memory/tests/conftest.py`** — Fixtures for fakeredis backend and mock Postgres backend
**`agent-memory/tests/test_tuplespace.py`** — out/rd/in_/count/expire with fakeredis
**`agent-memory/tests/test_tags.py`** — normalize_tags, suggest_extensions

### Verification
- `docker compose up` starts 4 services, all healthy
- `uv run pytest` in both `agent-memory/` and root pass
- `task minion:preflight` with `MEMORY_ENABLED=true` shows redis PASS
- Postgres has vector + age extensions, knowledge graph exists
- `MEMORY_ENABLED=false` (default): zero code path changes, all existing tests pass

---

## Phase 2: L2 Tuplespace (Redis Backend)

**Goal:** Fully working tuplespace with Redis backend. MCP tools for agents. Tool definitions per role.

### New files

**`agent_memory/backends/redis.py`** — Full implementation of `RedisTupleSpaceBackend`

### Existing files to modify

**`minions/server/mcp.py`** — Add 3 MCP tools inside `create_server()` (gated on `memory_enabled`):
- `publish_fact(project, category, key, value, tags, job_id, agent_role)` — calls `tuplespace.out()`
- `query_facts(project, category, key_pattern, tags, limit)` — calls `tuplespace.rd()`
- `create_memory_note(content, tags, project, links, job_id, agent_role)` — writes to L2 + queues for L3

**`minions/agents/tools/definitions.py`** — Add `_MEMORY_TOOLS` list (3 tool schemas). Modify `get_tools_for_role()` to accept `memory_enabled` flag and append memory tools.

**`minions/agents/tools/mcp_executor.py`** — Add memory tools to `_STATE_TOOL_INJECTIONS` with project/job_id/agent_role injection. Add `project_name` to `McpToolExecutor.__init__()`.

**`minions/cli.py`** — In `_run_server()`: create Redis connection, create TupleSpace, pass to `create_server()`.

**`minions/engine/job_engine.py`** — Store tuplespace reference. Pass `memory_enabled` to tool resolution.

### Tests

**`agent-memory/tests/test_tuplespace.py`** — Full Linda semantics tests with fakeredis
**`tests/agents/tools/test_memory_tools.py`** — `get_tools_for_role(role, memory_enabled=True)` includes memory tools

### Verification
- Agent calls `publish_fact` → fact appears in Redis
- Another agent calls `query_facts` → sees the published fact
- `in_()` atomically consumes a tuple (test with concurrent access)
- Facts scoped per project, not per job
- `MEMORY_ENABLED=false`: memory tools don't appear in tool lists

---

## Phase 3: L3 Knowledge Graph (Postgres Backend)

**Goal:** Persistent knowledge graph with MAGMA-style retrieval.

### New files

**`agent_memory/backends/postgres.py`** — Full implementation of `PostgresMemoryBackend`
**`agent_memory/retrieval.py`** — `get_relevant_memories()`, `get_file_backlinks()`, `_score_and_rank()`, `_budget_tokens()`
**`agent_memory/decay.py`** — `apply_decay(store, project, threshold_days)`

### Tests

**`agent-memory/tests/test_store.py`** — CRUD, tag queries, similarity search (mock embeddings), backlinks, entity dedup
**`agent-memory/tests/test_retrieval.py`** — Scoring, ranking, token budgeting with mock store

### Verification
- Create nodes with embeddings, query by similarity returns ranked results
- Tag-based filtering via GIN index works
- Backlinks: create node linking to "auth-module" → `get_backlinks("auth-module")` returns it
- Access count increments on retrieval
- Decay reduces salience of old unaccessed notes

---

## Phase 4: Prompt Integration

**Goal:** Inject L3 knowledge into agent prompts. Backlink-driven injection when touching files.

### New files

**`agent_memory/context.py`** — Framework-level context builders:
- `build_knowledge_context(store, project, task_description, embedding, max_tokens) -> str`
- `build_file_context(store, project, file_paths, max_tokens) -> str`
- Returns Obsidian-style markdown (tags, links, source attribution, local graph view)
- Returns empty string when no relevant knowledge found

### Existing files to modify

**`minions/agents/prompt.py`** — Add optional `knowledge_context: str | None` parameter to `build_agent_prompt()` and `build_prompt()`. Insert between task context and additional context sections.

**`minions/engine/dev.py`** — In `run_engineer()`, before launching agent:
```python
if engine.memory_store and config.memory_enabled:
    knowledge_ctx = await build_knowledge_context(engine.memory_store, project, task.description)
```
Pass as part of `context` parameter.

**`minions/engine/review.py`** — In `run_review_in_process()`, after fetching `changed_files`:
```python
if engine.memory_store and config.memory_enabled:
    file_ctx = await build_file_context(engine.memory_store, project, changed_files)
```
Prepend to context.

**`minions/engine/job_engine.py`** — Add `memory_store: MemoryStore | None` to `__init__()`.

### Tests

**`agent-memory/tests/test_context.py`** — Empty string when no notes, proper markdown formatting, token budget enforcement
**`tests/agents/test_prompt_with_memory.py`** — `build_agent_prompt()` includes "Prior Knowledge" section when knowledge_context provided

### Verification
- Engineer agent gets "Prior Knowledge" section when L3 has relevant notes
- Code reviewer gets "File Knowledge" section for files under review
- Token budget stays within configured limit
- No extra prompt sections when `MEMORY_ENABLED=false` or no relevant memories

---

## Phase 5: L2→L3 Archival

**Goal:** Archive L2 facts to L3 on job completion. Async causal inference via batch API.

### New files

**`agent_memory/archiver.py`** — `MemoryArchiver` class:
- `archive_job(tuplespace, store, job_id, project) -> int` — fast path: read L2 facts, create L3 nodes + temporal edges + entity edges
- `schedule_causal_inference(store, node_ids, project) -> str | None` — submit Anthropic Message Batches request
- `process_causal_batch(store, batch_id) -> int` — parse results, create causal edges

### Existing files to modify

**`minions/engine/job_engine.py`** — In `_on_job_terminal()`, trigger `archiver.archive_job()`. Add periodic poll for completed causal batches in the main loop.

**`minions/cli.py`** — Create `MemoryArchiver` on startup, pass to JobEngine.

### Tests

**`agent-memory/tests/test_archiver.py`** — Facts read from L2, written to L3, temporal edges in correct order, L2 cleaned up

### Verification
- Job completes → L2 facts archived to L3 with temporal + entity edges
- Next job on same project gets archived knowledge in prompt
- Causal batch submitted (verify via logs/mock)
- `MEMORY_ENABLED=false`: no archival runs

---

## Dependency Order

```
Phase 1 (skeleton + infra)
    ├──► Phase 2 (L2 tuplespace) ──┐
    └──► Phase 3 (L3 graph)  ──────┤
                                    ├──► Phase 4 (prompt integration)
                                    └──► Phase 5 (archival)
```

Phases 2 and 3 can be developed in parallel after Phase 1.

---

## Critical Files Reference

| File | Role |
|------|------|
| `minions/connectors/nats_client.py` | Pattern to follow for Redis client lifecycle |
| `minions/connectors/nats_config.py` | Pattern for RedisConfig dataclass |
| `minions/connectors/nats_init.py` | Pattern for idempotent index setup |
| `minions/db/abstract.py` | Pattern for Protocol-based backend interfaces |
| `minions/agents/tools/definitions.py` | Where to add `_MEMORY_TOOLS` and modify `get_tools_for_role()` |
| `minions/agents/tools/mcp_executor.py` | Where to add memory tool injections (`_STATE_TOOL_INJECTIONS`) |
| `minions/agents/prompt.py` | Where to inject knowledge context into `build_agent_prompt()` |
| `minions/server/mcp.py` | Where to register `publish_fact`, `query_facts`, `create_memory_note` tools |
| `minions/engine/job_engine.py` | Where to store memory_store/archiver refs, trigger archival |
| `minions/engine/dev.py` | Where to build knowledge context before launching engineers |
| `minions/engine/review.py` | Where to build file context before launching reviewers |
| `minions/config.py` | Where to add `memory_enabled`, `redis_url`, etc. |

---

## Verification (End-to-End)

1. `uv sync` in both `agent-memory/` and root — deps resolve
2. `uv run pytest` in `agent-memory/` — framework tests pass (fakeredis, mock postgres)
3. `uv run pytest` in root — all 372+ existing tests pass, plus new integration tests
4. `docker compose up` — 4 services healthy (app, postgres with AGE+pgvector, nats, redis)
5. `MEMORY_ENABLED=false` — zero behavior change, zero regression
6. `MEMORY_ENABLED=true`:
   - Agent publishes fact → visible to other agents on same project
   - Job completes → facts archived to L3
   - Next job → agent prompt includes "Prior Knowledge" section
   - Code reviewer → prompt includes "File Knowledge" for reviewed files
7. `task fmt && task lint` — code passes ruff formatting and lint
