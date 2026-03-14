## Why

Minion Suite agents are stateless between invocations — every job starts from scratch with zero knowledge of prior reviews, deployments, or decisions on the same project. This means agents repeatedly rediscover the same patterns, miss cross-job context (e.g., "this file was flagged in the last 3 reviews"), and cannot coordinate shared state during multi-agent orchestration. A tiered memory system gives agents persistent, queryable knowledge that compounds over time.

## What Changes

- **New standalone package `agent-memory`** in the monorepo — a framework-grade, backend-pluggable memory system with Protocol-based interfaces usable by any agent system, not just Minion Suite.
- **L2 shared cache (TupleSpace)** — Redis-backed Linda-style tuplespace for real-time fact sharing between agents within and across jobs. Agents can `out` (publish), `rd` (query), and `in_` (atomic consume) facts scoped per project.
- **L3 persistent knowledge graph** — Postgres-backed (AGE + pgvector) knowledge graph storing memory nodes with embeddings, entity links, backlinks, and access-frequency decay. MAGMA-style retrieval scores and ranks memories by relevance.
- **Prompt integration** — Inject relevant L3 knowledge into agent prompts: "Prior Knowledge" sections for engineers, "File Knowledge" sections for code reviewers (backlink-driven based on changed files).
- **L2-to-L3 archival** — On job completion, archive L2 facts into L3 nodes with temporal and entity edges. Optional async causal inference via Anthropic Message Batches API.
- **MCP tools for agents** — `publish_fact`, `query_facts`, `create_memory_note` tools gated behind `MEMORY_ENABLED` feature flag.
- **Infrastructure additions** — Redis (redis-stack) added to Docker Compose; Postgres image swapped to AGE with pgvector; new SQL migrations for memory tables and graph extensions.
- **Feature-flagged** — All memory functionality gated behind `MEMORY_ENABLED=false` (default). Zero code path changes when disabled.

## Capabilities

### New Capabilities
- `memory-framework`: Core `agent-memory` package — types (MemoryNode, Fact, Entity), Protocol interfaces (TupleSpaceBackend, MemoryStoreBackend, EmbeddingProvider), tag normalization, and public API surface.
- `tuplespace`: L2 TupleSpace with Linda primitives (out/rd/in_/watch), Redis backend implementation, and MCP tool exposure (publish_fact, query_facts).
- `knowledge-graph`: L3 MemoryStore with Postgres+AGE+pgvector backend, MAGMA-style retrieval, similarity search, backlinks, entity dedup, and access-frequency decay.
- `memory-context`: Prompt context builders — build_knowledge_context and build_file_context producing Obsidian-style markdown, integrated into agent prompt pipeline.
- `memory-archival`: L2-to-L3 archival on job completion, temporal/entity edge creation, and optional async causal inference via batch API.
- `memory-infra`: Docker Compose changes (Redis, AGE image), SQL migrations, config fields, preflight checks, and feature flag wiring.

### Modified Capabilities
<!-- No existing specs to modify -->

## Impact

- **New package**: `agent-memory/` directory with its own `pyproject.toml`, added as path dependency to `minions/pyproject.toml`
- **Docker stack**: Grows from 3 to 4 services (adds Redis); Postgres image changes from `postgres:17` to `apache/age:PG17_latest`
- **Config**: New env vars — `MEMORY_ENABLED`, `REDIS_URL`, `REDIS_PASSWORD`, `MEMORY_L3_TOKEN_BUDGET`
- **Dependencies**: `redis[hiredis]`, `fakeredis`, `litellm` (for embeddings) added; `psycopg` already present
- **Modified files**: `config.py`, `cli.py`, `job_engine.py`, `dev.py`, `review.py`, `prompt.py`, `mcp.py`, `definitions.py`, `mcp_executor.py`, `preflight.py`, `docker-compose.yml`, `.env.example`
- **Database**: 2 new migrations — extensions (vector, age) and memory tables (memory_nodes, memory_links, memory_entities)
- **Zero regression risk**: All new code paths gated behind `MEMORY_ENABLED` flag (default: false)
