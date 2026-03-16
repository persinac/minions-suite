# Agent Memory System — Architecture

## Overview

The agent-memory system is a three-tier memory architecture that gives agents persistent, cross-session knowledge. Agents learn from prior work — a code reviewer remembers patterns it flagged before, an engineer recalls architectural decisions from previous jobs.

The system is vendor-agnostic (backends are pluggable), fully observable (every operation emits trace events), and designed for multi-project isolation.

```
                  Agent Prompt
                      |
              +-------v--------+
              | Context Builder |  (context.py)
              | Obsidian-style  |
              | markdown inject |
              +---+--------+---+
                  |        |
          +-------v--+  +--v----------+
          | Retrieval |  | File        |
          | MAGMA     |  | Backlinks   |  (retrieval.py)
          | scoring   |  |             |
          +-----+-----+  +------+------+
                |               |
        +-------v---------------v-------+
        |         MemoryStore (L3)      |
        |   Postgres + pgvector + AGE   |  (store.py)
        |   Nodes, Entities, Links      |
        +---------------^--------------+
                        |
                   Archive (L2 -> L3)
                        |              (archiver.py)
        +---------------v--------------+
        |       TupleSpace (L2)        |
        |     Redis + RediSearch       |  (tuplespace.py)
        |    Facts, TTL, Categories    |
        +------------------------------+
```

## Tier Model

### L1 — Ephemeral (LLM Context Window)

Not managed by the memory system. This is the conversation history within a single agent invocation — the messages array passed to LiteLLM. Exists only for the duration of one `run_agent()` call.

### L2 — TupleSpace (Redis)

Short-lived facts shared between agents during a single job. Based on the Linda coordination model with three operations:

| Operation | Linda | Description |
|-----------|-------|-------------|
| `out()` | OUT | Publish a fact (non-blocking write) |
| `rd()` | RD | Read matching facts without removing them |
| `in_()` | IN | Atomically read and delete a fact |

Facts are scoped to a project and typically expire after job completion. Each fact has:

```python
Fact(
    category="decision",          # namespace (decision, error, finding, pattern)
    key="auth-approach",          # identifier within category
    value="Using JWT with...",    # content
    tags=["auth", "security"],    # controlled vocabulary
    agent_role="spec_analyst",    # who wrote it
    job_id="abc123",              # which job
    project="payments-api",       # project scope
    ttl=3600,                     # optional auto-expiry (seconds)
)
```

**Backend**: Redis with RedisJSON for document storage and RediSearch for indexed queries. The `in_()` operation uses a Lua script for atomicity (search + get + delete in one server round-trip).

### L3 — Knowledge Graph (Postgres)

Persistent knowledge that survives job completion. Three entity types:

**Nodes** — Knowledge units (a pattern observed, a decision made, a bug found):
```python
MemoryNode(
    content="JWT refresh token rotation...",
    title="auth:jwt-rotation-pattern",
    tags=["auth", "pattern", "security"],
    embedding=[0.12, -0.34, ...],     # 1536-dim via text-embedding-3-small
    source_job_id="abc123",
    source_agent_role="backend_engineer",
    project="payments-api",
    access_count=7,                    # popularity tracking
)
```

**Entities** — Named things (files, modules, concepts):
```python
Entity(name="src/auth.py", entity_type="file", project="payments-api")
```

**Links** — Directed edges between nodes and entities:
```python
Link(from_node="node-abc", to_entity="src/auth.py", link_type="mentions", confidence=1.0)
Link(from_node="node-abc", to_entity="node-def", link_type="FOLLOWS", confidence=1.0)
Link(from_node="node-ghi", to_entity="node-abc", link_type="CAUSED_BY", confidence=0.8)
```

**Storage**: Postgres with pgvector (cosine similarity on embeddings), GIN indexes (array overlap on tags), and Apache AGE (graph traversal via Cypher — future).

## Data Flow

### During a Job

```
1. Spec Analyst runs
   |-- publishes findings to L2 via tuplespace.out()

2. Arbiter runs
   |-- reads L2 facts via tuplespace.rd()
   |-- decides on engineer allocation

3. Engineers run
   |-- L3 knowledge injected into prompt via build_knowledge_context()
   |-- publish new findings to L2 during execution

4. Code Reviewer runs
   |-- L3 file-linked knowledge injected via build_file_context()
   |-- posts inline comments

5. Job completes (DONE or FAILED)
   |-- archiver.archive_job() promotes L2 facts to L3 nodes
   |-- entity extraction creates file/module entities
   |-- temporal edges (FOLLOWS) link consecutive observations
   |-- L2 facts deleted (cleanup)
```

### Archival Pipeline (L2 -> L3)

When a job reaches terminal state, the `MemoryArchiver` runs:

1. Read all L2 facts for the job from Redis
2. Sort by timestamp (temporal ordering)
3. For each fact:
   - Create a `MemoryNode` with content, tags, provenance
   - Generate embedding via LiteLLM (optional, skipped on error)
   - Extract entities from content using regex (file paths, module names)
   - Create entity nodes via `ensure_entity()` (idempotent)
   - Create `mentions` links from node to entities
4. Create `FOLLOWS` edges between consecutive nodes (temporal chain)
5. Delete archived facts from L2
6. Record `memory_archived` event in job history

### Retrieval Pipeline

When building an agent prompt, the retrieval system scores and ranks candidates:

1. **Gather candidates** — query L3 by tags and/or embedding similarity (up to 30 each)
2. **Score each candidate** using composite MAGMA-style formula:

```
score = 0.35 * sim_score      # semantic similarity (placeholder: 0.5)
      + 0.20 * tag_score      # Jaccard overlap with query tags
      + 0.20 * recency_score  # exp(-age_days / 30), half-life ~21 days
      + 0.15 * access_score   # log1p(access_count) / log1p(100), capped at 1.0
      + 0.10 * link_score     # min(1.0, num_links / 5), density proxy
```

3. **Token budget** — greedily select top-scored nodes until `max_tokens * 4` characters consumed
4. **Format as markdown** — Obsidian-style with `### Title`, `#tags`, `[[entity links]]`

### Context Injection

Two context builders format retrieved memories for prompt injection:

**`build_knowledge_context()`** — General knowledge relevant to the task:
```markdown
## Prior Knowledge

Relevant knowledge from prior work on this project:

### auth:jwt-rotation-pattern
Tags: #auth #pattern #security
JWT refresh token rotation should use sliding window...

Links: [[src/auth.py]], [[UserService]]
_Source: backend_engineer_
```

**`build_file_context()`** — Knowledge linked to specific changed files:
```markdown
## File Knowledge

Known context for files under review:

### decision:auth-middleware-placement
Tags: #auth #middleware #decision
Auth middleware must run before rate limiting...

Links: [[src/middleware/auth.py]]
_Source: code_reviewer_
```

## Observability

Every memory operation emits a `MemoryTraceEvent` with tier, operation, project, job_id, duration, and operation-specific details. Three callback sinks are wired in `cli.py`:

### OTEL Spans (Langfuse)

Each event becomes an OTEL span named `memory.{op}` (e.g., `memory.l2.put`, `memory.retrieval.result`). Spans carry attributes like `memory.tier`, `memory.project`, `memory.duration_ms`, and flattened detail fields. These appear nested under job traces in the Langfuse UI.

### Prometheus Metrics

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `memory_ops_total` | Counter | tier, op, project | Total operations |
| `memory_op_duration_seconds` | Histogram | tier, op | Operation latency |
| `memory_retrieval_candidates` | Histogram | — | Candidates scored per retrieval |
| `memory_retrieval_selected` | Histogram | — | Results selected per retrieval |

Scraped via `/metrics` endpoint on the dashboard (port 8322).

### Postgres Persistence

Events are buffered in a deque (max 500) and flushed to `minions.memory_operations` every 20 events or 5 seconds. The operations timeline on the dashboard `/memory` page reads from this table.

### Dashboard (`/memory`)

Three sections, all with HTMX auto-refresh:

- **Summary cards** — Node count, entity count, link count, operations count, per-project breakdown
- **Knowledge graph** — vis.js force-directed graph visualization with project selector. Knowledge nodes (blue dots) and entity nodes (purple diamonds) connected by labeled edges.
- **Operations timeline** — Recent operations color-coded by tier (L2=cyan, L3=blue, retrieval=yellow, context=green, archive=purple)

### Trace Operations Reference

| Tier | Operation | Key Details |
|------|-----------|-------------|
| L2 | `l2.put` | category, key, ttl |
| L2 | `l2.read` | category, result_count, duration_ms |
| L2 | `l2.consume` | found (bool), duration_ms |
| L2 | `l2.expire` | removed count |
| L3 | `l3.create_node` | node_id, tags, has_embedding |
| L3 | `l3.query_tags` | tags, result_count, duration_ms |
| L3 | `l3.query_similarity` | result_count, duration_ms |
| L3 | `l3.backlinks` | entity, result_count, duration_ms |
| Retrieval | `retrieval.score` | per-node composite + component scores |
| Retrieval | `retrieval.budget` | candidates, selected, max_tokens |
| Retrieval | `retrieval.result` | query_tags, result_count, duration_ms |
| Context | `context.knowledge` | chars, node_count |
| Context | `context.file` | chars, node_count, file_count |
| Archive | `archive.start` | fact_count |
| Archive | `archive.complete` | nodes, edges, entities, duration_ms |

## Tag System

Tags use a controlled vocabulary defined in `agent_memory/tags.py`. Tags are normalized (lowercase, deduplicated) and grouped into three categories:

- **Domain**: `api`, `auth`, `database`, `frontend`, `backend`, `testing`, `config`, `security`, `performance`
- **Action**: `pattern`, `decision`, `bug`, `fix`, `refactor`, `feature`, `risk`, `debt`, `breaking-change`
- **Outcome**: `approved`, `rejected`, `resolved`, `merged`, `reverted`

A `suggest_extensions()` function proposes related tags (e.g., `auth` -> `security`, `authentication`).

## Decay

Nodes age out via two mechanisms:

1. **Passive decay** — The retrieval scoring formula naturally penalizes old nodes via `recency_score = exp(-age_days / 30)`. A 30-day-old node scores 0.37; a 90-day-old node scores 0.05.

2. **Active decay** (placeholder) — `apply_decay()` scans for nodes older than a threshold where `access_count < log2(age_days)`. Currently logs candidates but does not delete. Future: prune or move to cold storage.

## Schema

### Postgres Tables

```
minions.memory_nodes        — Knowledge nodes (content, tags, embedding, access_count)
minions.memory_links        — Directed edges (from_node, to_entity, link_type, confidence)
minions.memory_entities     — Named entities (name, entity_type, project)
minions.memory_operations   — Trace event persistence (op, tier, duration_ms, details JSONB)
```

### Postgres Extensions

- **pgvector** — `vector(1536)` column type, `<=>` cosine distance operator, IVFFlat index
- **Apache AGE** — Graph query engine (Cypher), `knowledge` graph created for future multi-hop traversal

### Redis Structures

- **Keys**: `fact:{project}:{fact_id}` — RedisJSON documents
- **Index**: `idx:facts:{project}` — RediSearch index with TAG, TEXT, NUMERIC fields

## Package Structure

```
agent-memory/
  agent_memory/
    __init__.py
    tuplespace.py        # L2 — Linda-model fact coordination
    store.py             # L3 — Knowledge graph CRUD
    retrieval.py         # MAGMA-style multi-signal scoring
    context.py           # Markdown context builders
    archiver.py          # L2→L3 promotion + entity extraction
    tracing.py           # MemoryTraceEvent + callback system
    decay.py             # Node aging / eviction candidates
    tags.py              # Controlled tag vocabulary
    embeddings.py        # LiteLLM embedding provider
    cli.py               # inspect, stats, trace, flush commands
    backends/
      redis.py           # RedisTupleSpaceBackend (RedisJSON + RediSearch)
      postgres.py        # PostgresMemoryBackend (psycopg + pgvector)

minions/
  observability/
    memory_otel.py       # OTEL span callback + Postgres persistence callback
    memory_metrics.py    # Prometheus counter/histogram callback
  dashboard.py           # /memory page, /api/memory/* endpoints, /metrics
```

## Configuration

Settings in `settings.toml` / `settings.local.toml`:
```toml
[default.memory]
enabled = false          # feature flag
l3_token_budget = 2000   # max tokens for context injection
log_level = "INFO"       # DEBUG for trace-level logs
```

Secrets in `.env`:
```
REDIS_URL=redis://localhost:6379
REDIS_PASSWORD=
POSTGRES_URL=postgresql://...   # shared with main app
```

## CLI Tools

```bash
task memory:inspect              # Show L2 facts + L3 nodes
task memory:inspect:l2           # L2 only (Redis)
task memory:inspect:l3           # L3 only (Postgres)
task memory:inspect:graph        # AGE graph structure
task memory:stats                # Per-project statistics
task memory:trace -- --project X --query "auth patterns"   # Trace a retrieval
task memory:flush:l2 -- project-name    # Flush L2 (destructive)
task memory:flush:all -- project-name   # Flush both tiers (destructive)
```
