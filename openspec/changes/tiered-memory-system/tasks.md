## 1. Package Skeleton & Protocols

- [ ] 1.1 Create `agent-memory/pyproject.toml` with package metadata, Python >=3.14, core dep (pydantic), optional extras (redis, psycopg, litellm), and test deps (pytest, pytest-asyncio, fakeredis)
- [ ] 1.2 Create `agent_memory/types.py` with Pydantic models: MemoryNode, Fact, Entity
- [ ] 1.3 Create `agent_memory/protocols.py` with @runtime_checkable Protocols: TupleSpaceBackend, MemoryStoreBackend, EmbeddingProvider
- [ ] 1.4 Create `agent_memory/tags.py` with CONTROLLED_TAGS vocabulary, normalize_tags(), suggest_extensions()
- [ ] 1.5 Create `agent_memory/embeddings.py` with LiteLLMEmbeddingProvider implementing EmbeddingProvider
- [ ] 1.6 Create `agent_memory/__init__.py` with public API re-exports
- [ ] 1.7 Write `agent-memory/tests/test_tags.py` — normalize_tags, suggest_extensions unit tests

## 2. Infrastructure & Config

- [ ] 2.1 Add Redis service to `docker-compose.yml` (redis/redis-stack:latest, healthcheck, port 6379, volume)
- [ ] 2.2 Swap Postgres image in `docker-compose.yml` to AGE+pgvector (investigate best base image)
- [ ] 2.3 Create SQL migration `20260314120000_add_memory_extensions.sql` — vector, age extensions + knowledge graph
- [ ] 2.4 Create SQL migration `20260314130000_create_memory_tables.sql` — memory_nodes, memory_links, memory_entities tables with indexes
- [ ] 2.5 Add memory config fields to `minions/config.py`: memory_enabled, redis_url, redis_password, memory_l3_token_budget
- [ ] 2.6 Update `.env.example` with MEMORY_ENABLED, REDIS_URL, REDIS_PASSWORD, MEMORY_L3_TOKEN_BUDGET
- [ ] 2.7 Add `check_redis()` to `minions/preflight.py` (gated on memory_enabled)
- [ ] 2.8 Add `agent-memory` as path dependency in `minions/pyproject.toml`

## 3. L2 TupleSpace

- [ ] 3.1 Create `agent_memory/tuplespace.py` — TupleSpace class with out(), rd(), in_(), count(), expire_project(), delegating to TupleSpaceBackend
- [ ] 3.2 Create `agent_memory/backends/redis.py` — RedisTupleSpaceBackend: connect, put (JSON.SET+EXPIRE), search (FT.SEARCH), atomic_pop (Lua script), index creation
- [ ] 3.3 Create `agent-memory/tests/conftest.py` with fakeredis fixtures and mock backends
- [ ] 3.4 Write `agent-memory/tests/test_tuplespace.py` — out/rd/in_/count/expire_project with fakeredis backend

## 4. L3 Knowledge Graph

- [ ] 4.1 Create `agent_memory/store.py` — MemoryStore class delegating CRUD to MemoryStoreBackend
- [ ] 4.2 Create `agent_memory/backends/postgres.py` — PostgresMemoryBackend: create_node, get_node, query_by_tags, query_by_similarity, create_link, get_backlinks, ensure_entity, increment_access
- [ ] 4.3 Create `agent_memory/retrieval.py` — get_relevant_memories(), get_file_backlinks(), _score_and_rank(), _budget_tokens() with MAGMA-style scoring
- [ ] 4.4 Create `agent_memory/decay.py` — apply_decay(store, project, threshold_days)
- [ ] 4.5 Write `agent-memory/tests/test_store.py` — CRUD, tag queries, similarity search (mock embeddings), backlinks, entity dedup
- [ ] 4.6 Write `agent-memory/tests/test_retrieval.py` — scoring, ranking, token budgeting with mock store

## 5. MCP Tools & Agent Tool Definitions

- [ ] 5.1 Add `_MEMORY_TOOLS` schemas to `minions/agents/tools/definitions.py` (publish_fact, query_facts, create_memory_note)
- [ ] 5.2 Modify `get_tools_for_role()` to accept `memory_enabled` flag and append memory tools when True
- [ ] 5.3 Register publish_fact, query_facts, create_memory_note in `minions/server/mcp.py` (gated on memory_enabled)
- [ ] 5.4 Add memory tools to `_STATE_TOOL_INJECTIONS` in `minions/agents/tools/mcp_executor.py` with project/job_id/agent_role injection
- [ ] 5.5 Write `tests/agents/tools/test_memory_tools.py` — get_tools_for_role with memory_enabled=True/False

## 6. Prompt Integration

- [ ] 6.1 Create `agent_memory/context.py` — build_knowledge_context() and build_file_context() producing Obsidian-style markdown
- [ ] 6.2 Add optional `knowledge_context` param to `build_agent_prompt()` and `build_prompt()` in `minions/agents/prompt.py`
- [ ] 6.3 Wire knowledge context in `minions/engine/dev.py` — call build_knowledge_context() before launching engineers when memory_enabled
- [ ] 6.4 Wire file context in `minions/engine/review.py` — call build_file_context() with changed_files before launching reviewers when memory_enabled
- [ ] 6.5 Add `memory_store: MemoryStore | None` to JobEngine.__init__() in `minions/engine/job_engine.py`
- [ ] 6.6 Write `agent-memory/tests/test_context.py` — empty string when no notes, markdown formatting, token budget
- [ ] 6.7 Write `tests/agents/test_prompt_with_memory.py` — build_agent_prompt includes "Prior Knowledge" section when knowledge_context provided

## 7. L2→L3 Archival

- [ ] 7.1 Create `agent_memory/archiver.py` — MemoryArchiver with archive_job() (fast path: L2 facts → L3 nodes + temporal/entity edges)
- [ ] 7.2 Implement schedule_causal_inference() — submit Anthropic Message Batches request for causal link inference
- [ ] 7.3 Implement process_causal_batch() — parse batch results, create causal edges
- [ ] 7.4 Trigger archival in `minions/engine/job_engine.py` _on_job_terminal() when memory_enabled
- [ ] 7.5 Add periodic causal batch polling to JobEngine main loop
- [ ] 7.6 Write `agent-memory/tests/test_archiver.py` — facts archived to L3, temporal edges in order, L2 cleanup

## 8. CLI Wiring & Integration

- [ ] 8.1 Create Redis connection and TupleSpace in `minions/cli.py` _run_server() when memory_enabled
- [ ] 8.2 Create MemoryArchiver and pass to JobEngine in cli.py
- [ ] 8.3 Pass memory_enabled and tuplespace to create_server() in mcp.py
- [ ] 8.4 Store tuplespace reference in JobEngine, pass memory_enabled to tool resolution

## 9. Verification & Cleanup

- [ ] 9.1 Run `uv run pytest` in `agent-memory/` — all framework tests pass
- [ ] 9.2 Run `uv run pytest` in root — all 372+ existing tests pass, no regressions
- [ ] 9.3 Run `task fmt && task lint` — code passes ruff formatting and lint
- [ ] 9.4 Verify `docker compose up` starts all 4 services healthy
- [ ] 9.5 Verify MEMORY_ENABLED=false produces zero behavior change
