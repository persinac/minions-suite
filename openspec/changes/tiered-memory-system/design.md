## Context

Minion Suite agents are stateless — each job invocation starts fresh with no memory of prior work on the same project. This limits agent effectiveness: code reviewers can't recall recurring issues, engineers can't leverage lessons from previous tasks, and multi-agent orchestration has no shared scratchpad beyond NATS messages.

The existing infrastructure provides building blocks: Postgres (via psycopg3 async pool) for persistence, NATS for pub/sub, and a Protocol-based backend pattern (see `db/abstract.py`). The memory system builds on these patterns while introducing Redis for real-time shared state and extending Postgres with AGE (graph) and pgvector (embeddings).

The system is designed as a standalone package (`agent-memory`) so it can be reused outside Minion Suite. All external backends are behind Protocol interfaces.

## Goals / Non-Goals

**Goals:**
- Give agents persistent, project-scoped knowledge that compounds across jobs
- Enable real-time fact sharing between concurrent agents (L2 tuplespace)
- Store long-term knowledge as a queryable graph with semantic search (L3 knowledge graph)
- Automatically inject relevant knowledge into agent prompts without agent initiative
- Keep the system fully opt-in — zero behavior change when `MEMORY_ENABLED=false`
- Package as a reusable library with pluggable backends

**Non-Goals:**
- Cross-project knowledge sharing (all memory is project-scoped)
- Agent-to-agent direct messaging (NATS already handles this)
- Real-time streaming/subscription from L3 (L3 is batch-queried at prompt build time)
- Custom UI for memory exploration (may come later)
- Fine-tuning or training on memory data
- Supporting non-Python consumers of the `agent-memory` package

## Decisions

### 1. Two-tier architecture (L2 + L3) over single store

**Decision:** Separate fast ephemeral cache (L2/Redis) from slow persistent graph (L3/Postgres).

**Rationale:** Agent coordination needs sub-millisecond reads with TTL expiry (Redis excels here). Long-term knowledge needs graph traversal, vector similarity, and ACID guarantees (Postgres excels here). A single store would compromise on one dimension.

**Alternatives considered:**
- *Single Postgres store for everything* — too slow for real-time coordination; no native TTL/expiry
- *NATS KV for L2* — already in stack but lacks RediSearch-style indexing and Lua scripting for atomic operations
- *Dedicated graph DB (Neo4j)* — adds operational burden; AGE gives Cypher on existing Postgres

### 2. Linda tuplespace model for L2

**Decision:** Use Linda coordination primitives (`out`, `rd`, `in_`) rather than pub/sub or key-value.

**Rationale:** Linda's `in_` (atomic read-and-delete) enables work-stealing patterns — e.g., an agent can atomically claim a fact so no other agent processes it. `rd` allows non-destructive queries. This maps naturally to multi-agent coordination where agents need both shared reads and exclusive claims.

**Alternatives considered:**
- *Plain key-value* — no atomic consume, no query-by-pattern
- *NATS JetStream consumers* — good for queues but poor for ad-hoc queries across fact categories

### 3. AGE + pgvector on existing Postgres over separate graph/vector DBs

**Decision:** Extend the existing Postgres instance with AGE (Apache Graph Extension) for Cypher queries and pgvector for embedding similarity search.

**Rationale:** Minimizes operational surface — one database to back up, monitor, and scale. AGE provides Cypher graph traversal on top of relational tables. pgvector provides ANN similarity search. Both are mature extensions.

**Alternatives considered:**
- *Neo4j + Pinecone* — best-in-class but doubles infra cost and ops burden
- *pgvector alone (no graph)* — loses traversal queries (backlinks, causal chains)
- *SQLite for dev graph* — AGE has no SQLite equivalent; would need two code paths

**Fallback:** If AGE proves problematic, graph queries fall back to recursive CTEs on relational tables. The Protocol interface isolates this decision.

### 4. MAGMA-style retrieval scoring

**Decision:** Score memories using a weighted combination of: semantic similarity (embedding distance), tag overlap, recency, access frequency, and link density.

**Rationale:** Pure vector search misses structured relationships. Pure tag search misses semantic meaning. Combining multiple signals with configurable weights gives the best relevance ranking for agent prompts.

### 5. Standalone package with Protocol-based backends

**Decision:** Ship `agent-memory` as an independent package in the monorepo with all backends behind `@runtime_checkable` Protocol classes.

**Rationale:** Follows the existing pattern in `db/abstract.py`. Makes it possible to swap Redis for NATS KV, or Postgres for another graph store, without touching framework code. Also enables the package to be extracted to its own repo later.

### 6. Feature flag gating

**Decision:** All memory code paths gated behind `Config.memory_enabled` (env: `MEMORY_ENABLED`, default: `false`).

**Rationale:** Zero-risk rollout. Existing deployments see no change. Memory can be enabled per-environment. Tool lists, prompt sections, archival hooks — all conditional.

### 7. Archival via fast path + async causal inference

**Decision:** On job completion, immediately archive L2 facts to L3 nodes (fast path). Optionally submit an Anthropic Message Batches request for causal link inference (slow path).

**Rationale:** Fast path ensures no knowledge is lost even if batch API is unavailable. Causal inference enriches the graph but is not on the critical path. Batch API is cost-effective for bulk inference.

## Risks / Trade-offs

**[Redis as new infrastructure dependency]** → Mitigated by feature flag. When `MEMORY_ENABLED=false`, Redis is not required. For dev, `fakeredis` provides a zero-infra test path.

**[AGE extension maturity]** → AGE on PG17 is relatively new. → Mitigated by fallback to recursive CTEs in the Postgres backend. Protocol interface isolates the graph query strategy.

**[Embedding cost]** → Every `create_memory_note` call generates an embedding via LiteLLM. → Mitigated by batching at archival time rather than per-fact, and by making the embedding provider pluggable (could use local models).

**[Token budget for prompt injection]** → Injecting too much knowledge bloats prompts. → Mitigated by configurable `MEMORY_L3_TOKEN_BUDGET` (default: 2000 tokens) and scoring/ranking to select only the most relevant memories.

**[fakeredis RediSearch compatibility]** → `fakeredis` may not fully support `FT.SEARCH`. → If gaps found, tests will use a thin mock layer over search operations. Documented in implementation plan.

**[Docker image for AGE + pgvector]** → `apache/age:PG17_latest` bundles AGE but not pgvector. May need a custom Dockerfile layering pgvector on top. → Investigate during Phase 1; fallback is `pgvector/pgvector:pg17` as base with AGE installed.

## Migration Plan

1. **Phase 1**: Ship package skeleton + infra (Docker, migrations, config). Feature flag defaults off. No behavior change.
2. **Phase 2-3** (parallel): Implement L2 tuplespace and L3 knowledge graph backends.
3. **Phase 4**: Wire prompt integration. Agents gain memory-aware prompts when flag is on.
4. **Phase 5**: Enable archival pipeline. Full loop: agents write facts → archived to graph → injected into future prompts.
5. **Rollback**: Set `MEMORY_ENABLED=false`. All memory code paths deactivate. No data loss (Redis/Postgres data persists for re-enablement).

## Open Questions

- **AGE + pgvector Docker image**: Need to validate whether `apache/age:PG17_latest` can have pgvector layered on, or if we need a custom Dockerfile. Resolve in Phase 1.
- **fakeredis FT.SEARCH support**: Need to confirm `fakeredis[json]` supports RediSearch commands. If not, determine mock strategy. Resolve in Phase 1.
- **Embedding model selection**: Default to `text-embedding-3-small` (1536 dims) via LiteLLM? Or allow project-level override in `projects.yaml`? Resolve in Phase 2.
- **Memory retention policy**: How long should L3 nodes live? Decay threshold? Per-project config? Resolve in Phase 3.
