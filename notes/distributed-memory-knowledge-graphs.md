# Distributed Memory, Knowledge Graphs, and Agent Coordination

**Date:** 2026-03-14  
**Status:** Research / Early Design  
**Context:** Exploring how tiered memory, knowledge graphs, tuplespaces, and inter-agent protocols could give Minion Suite agents shared, persistent, queryable memory with coherence guarantees.  

# Alex Notes

Layered memory and knowledge graphs can be analogous to the hippocampus: plays important roles in the consolidation of information from short-term memory to long-term memory, and in spatial memory that enables navigation  
Inspiration: orthogonalized state machine in the hippocampus (https://www.nature.com/articles/s41586-024-08548-w)

Orthogonal as "Independent Dimensions"
- orthogonal vectors are perpendicular — they share zero information with each other. You can change one without affecting the other

MAGMA's four graphs are orthogonal views of the same memory — four independent ways to index and traverse the same set of events, where each dimension captures something the others cannot:

The same memory node: "Refactored auth to JWT RS256"

Temporal:  WHEN did it happen?     ──► between node_41 and node_43 on the timeline  
Causal:    WHY did it happen?      ──► because spec required SSO (node_38)  
Semantic:  WHAT is it similar to?  ──► similar to "Added OAuth2 flow" (node_12)  
Entity:    WHO/WHAT was involved?  ──► auth-module, api-service, jwt-library  

These four questions are independent — knowing WHEN something happened tells you nothing about WHY. Knowing WHAT it's similar to tells you nothing about WHO was
involved. Each graph is a different lens on the same data, and you can query one without touching the others.

This is different from a single knowledge graph where temporal, causal, and semantic edges are all mixed together in one soup. The orthogonality means you can
weight the dimensions differently per query:

"Why did the auth module change?"    → upweight causal, downweight temporal
"What happened last Tuesday?"        → upweight temporal, downweight semantic
"What else looks like this pattern?" → upweight semantic, downweight causal
"Everything about the users table?"  → upweight entity, downweight temporal

MAGMA's scoring function makes this explicit:

S(neighbor | current, query) = exp(
  λ₁ · φ(edge_type, query_intent) +    ← structural alignment (which graph dimension?)
  λ₂ · sim(neighbor, query)              ← semantic affinity (embedding distance)
)

The φ function assigns higher weights to the graph dimension that matches the query intent. "Why" queries get high φ for causal edges. "When" queries get high φ
for temporal edges.

orthogonalized state representations in the hippocampus — the brain maintains independent neural codes for different aspects 
of experience. Position cells, time cells, and context cells fire independently. You can be in the same place at different times, or at different places at the 
same time — the representations don't interfere.

The analogy to agent memory:

Hippocampus                          Agent Memory
───────────                          ────────────
Place cells     → WHERE              Entity graph  → WHAT entity
Time cells      → WHEN               Temporal graph → WHEN it happened
Context cells   → situational state  Causal graph  → WHY (the context/reason)
Pattern cells   → similar patterns   Semantic graph → WHAT it resembles

The hippocampus consolidates short-term to long-term memory — exactly the L2 → L3 archival path. And it does so by maintaining these orthogonal dimensions during 
consolidation, so a memory can be retrieved via any dimension later.

---

## 1. The Problem

Today Minion Suite agents are stateless between invocations. An engineer agent gets a prompt, does work, and dies. The next agent (reviewer, revision engineer) starts fresh — it gets context injected via `build_checkpoint_summary()` and `get_review_feedback()`, but there's no persistent memory substrate agents can read/write during execution or across job boundaries.

What we want:
- Agents that **learn across jobs** (e.g., "last time I touched this repo's auth module, the reviewer flagged X")
- Agents that **share knowledge in real time** during a job (e.g., backend engineer discovers a schema constraint the frontend engineer needs)
- An **arbiter that sees the full knowledge state** across all running agents
- **Queryable causal/temporal history** — not just "what happened" but "why" and "when"

---

## 2. Research Landscape

### 2.1 Tiered Memory (L1/L2/L3 Cache Analogy)

**Source:** Yu et al., "Multi-Agent Memory from a Computer Architecture Perspective" (March 2026, arXiv:2603.10062)

Frames multi-agent memory as a computer architecture problem with three tiers:

| Tier | Analogy | Contents | Latency | Capacity |
|------|---------|----------|---------|----------|
| **L1 — Agent I/O** | CPU registers | Active context window, current tool call results, KV cache | ~0 (in-context) | Small (context window) |
| **L2 — Agent Cache** | L1/L2 cache | Compressed context, recent tool calls, embeddings, short-term latent storage | Low (local lookup) | Medium |
| **L3 — Agent Memory** | Main memory / disk | Full dialogue history, vector DBs, graph DBs, document stores | Higher (retrieval) | Large (unbounded) |

Key insights from the paper:
- Agent performance is "an end-to-end data movement problem"
- Caching is "not optional"
- **Missing protocol #1:** No principled protocol for sharing cached artifacts across agents (analogous to multiprocessor cache transfers)
- **Missing protocol #2:** No standard memory access protocol (permissions, scope, granularity)
- Consistency requires both **read-time conflict handling** and **update-time visibility ordering**

**MemGPT** (Packer et al., 2023) implements this practically:
- **Core Memory** — always in context, compressed essential facts (like L1)
- **Recall Memory** — searchable via semantic search, reconstructs specific memories (like L2)
- **Archival Memory** — long-term storage, moved back into core/recall on demand (like L3)
- The LLM itself manages data movement between tiers via tool calls

### 2.2 Multi-Graph Knowledge Architecture

**Source:** MAGMA (Jiang et al., January 2026, arXiv:2601.03236)

Instead of a single vector store, maintains **four orthogonal graphs** over the same memory items:

| Graph | Structure | Query Type | Example |
|-------|-----------|------------|---------|
| **Temporal** | Ordered pairs (n_i, n_j) where t_i < t_j | "When did X happen?" | Immutable timeline |
| **Causal** | Directed edges = logical entailment | "Why did X happen?" | Inferred during consolidation |
| **Semantic** | Undirected edges = cosine similarity > threshold | "What's related to X?" | Standard embedding similarity |
| **Entity** | Edges connecting events to entity nodes | "What happened to entity X?" | Object permanence across time |

Each memory node stores: content, timestamp, dense embedding (vector), structured attributes.

**Retrieval** is policy-guided traversal:
1. Query decomposition (intent: why/when/entity + temporal parsing + embeddings)
2. Anchor identification via Reciprocal Rank Fusion
3. Adaptive beam search: `S(n_j|n_i,q) = exp(lambda_1 * phi(edge_type, query_intent) + lambda_2 * sim(n_j, q))`
4. Narrative synthesis with topological ordering and token budgeting

**Dual-stream writes:**
- **Fast path (synaptic):** event segmentation, vector indexing, temporal backbone — non-blocking
- **Slow path (consolidation):** background worker infers causal/entity edges via LLM over 2-hop neighborhoods

Results: 45.5% higher reasoning accuracy, 95% token reduction vs full-context, 40% faster query latency.

### 2.3 Agentic Memory (Zettelkasten-inspired)

**Source:** A-MEM (February 2025, arXiv:2502.12110)

Each memory item is a structured "note" with:
- Content
- LLM-generated keywords, tags, contextual descriptions
- Dynamically constructed links to semantically related memories

The agent itself decides what to store, how to link, and when to evolve existing memories. Not predetermined operations — agent-driven decision-making.

### 2.4 Coordination Paradigms for Agentic AI

**Source:** Borghoff, Bottoni, Pareschi — "Coordination and Communication Foundations for Agentic AI" (EUROCAST 2026)

| Model | Coordination | Communication | Agentic AI Relevance |
|-------|-------------|---------------|---------------------|
| **Linda** | Decentralized shared store | Async, generative | Loosely coupled collaboration, indirect data sharing |
| **Blackboard** | Centralized shared state | Controlled, opportunistic | Global state coordination, transparent reasoning |
| **Linear Objects (LO)** | Logical agents, linear logic | Message-driven, resource-aware | Communication as computation, state evolution |
| **CLF** | Logical framework for concurrency | Typed, concurrent semantics | Verified interleaving, compositional reasoning |
| **CCP** | Constraint-based shared store | Guarded, declarative sync | Coordination by entailment, knowledge accumulation |

Key connections to our architecture:

**Linda tuplespace** — Agents interact by producing (`out`) and consuming (`in`) tuples from a shared associative memory, decoupled in time and space. Non-destructive read (`rd`) allows observation without consumption. `eval` creates new processes. This is remarkably close to what NATS JetStream already provides — publish/subscribe with persistence — but Linda adds **pattern matching on tuple structure**, not just topic routing.

**Blackboard** — A centralized shared state where agents read/write opportunistically. Our DB + arbiter already approximates this. The arbiter monitors global state and can intervene.

**CCP (Concurrent Constraint Programming)** — Agents interact via a shared constraint store using `tell` (add constraint) and `ask` (block until constraint entailed). The store is **monotonic** — constraints accumulate, never retract. This maps to knowledge accumulation: once an agent discovers "the auth module uses JWT", that fact persists and other agents can query for it.

**Linear Objects** — Resources are consumed when used. An agent "claims" a task by consuming a tuple; it can't be double-claimed. This is exactly the PENDING → IN_PROGRESS transition problem we solve with `busy_services` checks today.

### 2.5 Agent-to-Agent Protocol (A2A)

**Source:** Google, April 2025 (now Linux Foundation, 150+ orgs)

Protocol for inter-agent communication:
- **Agent Cards** — JSON capability descriptors (like DNS SRV records for agents)
- **Task lifecycle** — defined states for work items exchanged between agents
- **Three modalities** — sync request/response, SSE streaming, async push notifications
- **Opacity** — agents don't expose internal state/memory/tools, only capabilities
- **Complements MCP** — MCP = agent-to-tool, A2A = agent-to-agent

Wire protocol: JSON-RPC 2.0 over HTTPS. v0.3 adds gRPC support.

---

## 3. Synthesis: A Tiered Memory Architecture for Minion Suite

### 3.1 The Three Tiers

```
┌─────────────────────────────────────────────────────────────────┐
│                        L1 — WORKING MEMORY                      │
│                                                                 │
│  Per-agent, in-context. Dies with the agent.                    │
│  - LLM context window (system prompt + messages)                │
│  - Current tool call results                                    │
│  - KV cache (managed by LiteLLM/provider)                       │
│                                                                 │
│  Managed by: the LLM itself (already exists)                    │
├─────────────────────────────────────────────────────────────────┤
│                        L2 — SHARED CACHE                        │
│                                                                 │
│  Per-job, shared across agents in the same job.                 │
│  Survives agent death. Fast read/write.                         │
│  - Discovered facts ("auth uses JWT", "DB schema has FK on X")  │
│  - File summaries (agent read a file, cached the summary)       │
│  - Tool call results (deduplication across agents)              │
│  - Inter-agent messages (already exists via Messages table)     │
│  - Active constraints / decisions                               │
│                                                                 │
│  Backed by: NATS KV store or Redis                              │
│  Access pattern: tuplespace (out/in/rd with pattern matching)   │
│  Coherence: eventual consistency, last-writer-wins per key      │
├─────────────────────────────────────────────────────────────────┤
│                        L3 — KNOWLEDGE GRAPH                     │
│                                                                 │
│  Cross-job, persistent. Survives across jobs.                   │
│  - Temporal graph: timeline of events across all jobs            │
│  - Causal graph: why decisions were made                        │
│  - Entity graph: repos, files, modules, patterns                │
│  - Semantic graph: embeddings for similarity search             │
│                                                                 │
│  Backed by: PostgreSQL + pgvector (or dedicated graph DB)       │
│  Access pattern: MAGMA-style policy-guided traversal            │
│  Write path: fast (temporal) + slow (causal/entity inference)   │
└─────────────────────────────────────────────────────────────────┘
```

### 3.2 Data Flow Between Tiers

```
Agent executing tool call
     │
     ▼
  L1 (context window)
     │
     ├── agent discovers fact ──► out("fact", "auth", "jwt") ──► L2 (shared cache)
     │                                                              │
     ├── agent needs info ──────► rd("fact", "auth", ?) ◄──────────┘
     │                           (pattern match, non-destructive)
     │
     ├── agent completes task ──► L3 write (fast path):
     │                           - temporal edge: task_t1 → task_t2
     │                           - entity edge: file_X → change_Y
     │                           (slow path runs async):
     │                           - causal inference: "changed auth because spec required SSO"
     │
     └── next job starts ──────► L3 read:
                                 "what do we know about this repo's auth module?"
                                 → MAGMA traversal across temporal + entity + causal graphs
                                 → inject into L1 as context
```

### 3.3 Linda Primitives Mapped to Agent Operations

```python
# Tuplespace operations (L2 shared cache)

# Agent discovers something → publish to shared space
await space.out(("fact", "service:api", "auth_mechanism", "jwt_rs256"))
await space.out(("fact", "service:api", "db_constraint", "users.email UNIQUE"))
await space.out(("file_summary", "src/auth.py", "JWT RS256 auth with refresh tokens, 152 lines"))

# Another agent needs to know → pattern match read (non-destructive)
auth_info = await space.rd(("fact", "service:api", "auth_mechanism", ?))
# Returns: ("fact", "service:api", "auth_mechanism", "jwt_rs256")

# Agent claims exclusive work → destructive read (consume)
task = await space.in(("available_task", "service:frontend", ?))
# Tuple is removed — no other agent can claim it

# Spawn a new process
await space.eval(("review_needed", task_id, pr_url))
# Creates a new agent/process to handle the review
```

### 3.4 Cache Coherence for Multi-Agent Systems

Drawing from MESI/MOESI but adapted for semantic data:

| State | Meaning | Agent Behavior |
|-------|---------|---------------|
| **Modified** | Agent has written new knowledge not yet visible to others | Flush to L2 on next heartbeat or tool call boundary |
| **Exclusive** | Agent is the only one who has read this fact | Can modify without coordination |
| **Shared** | Multiple agents have read this fact | Must publish update to L2 if modified |
| **Invalid** | Fact has been superseded by another agent's write | Re-read from L2 on next access |

In practice, for LLM agents, strict MESI is overkill. Better model:

**Monotonic Knowledge Accumulation (CCP-inspired):**
- Facts are **additive** — once discovered, they persist
- Agents `tell` the shared store new facts
- Agents `ask` the store and block (or get empty result) until facts are available
- No retraction — if a fact is wrong, add a correction fact with higher timestamp
- Conflict resolution: latest timestamp wins, or arbiter adjudicates

This sidesteps the hardest cache coherence problems (invalidation, write-back ordering) by making the store append-only with versioning.

### 3.5 A2A for Long-Horizon Observability

The arbiter (or a human) should be able to observe any running agent's progress without interrupting it. A2A's Agent Card model provides this:

```json
{
  "name": "backend-engineer-abc123",
  "description": "Working on API auth refactor for job def456",
  "capabilities": ["code_write", "git_push", "test_run"],
  "status": {
    "current_subtask": "Implementing JWT refresh token rotation",
    "subtask_progress": "3/7 subtasks complete",
    "files_modified": ["src/auth.py", "src/middleware.py"],
    "facts_discovered": 4,
    "estimated_completion": "~5 min"
  },
  "task": {
    "id": "task_789",
    "state": "in_progress",
    "revision_count": 0
  }
}
```

An observer agent (or the arbiter's monitor loop) can `GET /agent-card` on any running agent to see its current state without interrupting execution. This is read-only observation of L1 state, exposed via A2A protocol.

Combined with L2 shared cache, the arbiter can:
1. Read Agent Cards to see what each agent is doing (A2A)
2. Read the shared tuplespace to see what facts have been discovered (Linda)
3. Query the knowledge graph for historical patterns (MAGMA)
4. Inject constraints or corrections via `tell` operations (CCP)

---

## 4. Mapping to Minion Suite's Existing Infrastructure

| Concept | Current Implementation | Proposed Evolution |
|---------|----------------------|-------------------|
| L1 (working memory) | LLM context window | No change needed |
| L2 (shared cache) | DB Messages table + heartbeats | NATS KV store with tuplespace semantics |
| L3 (knowledge graph) | None (agents are stateless across jobs) | PostgreSQL + pgvector multi-graph store |
| Agent observation | Heartbeat polling + log files | A2A Agent Cards via MCP server |
| Fact sharing | `send_message()` MCP tool (unstructured text) | Typed tuples with pattern matching |
| Coordination | Arbiter + NATS request/reply | Arbiter as tuplespace monitor + CCP constraint store |
| Cross-job learning | None | L3 knowledge graph injected into agent prompts |

### What NATS Already Gives Us

NATS JetStream is surprisingly close to a tuplespace:
- **KV Store** — key-value with watch (like `rd` with notifications)
- **Object Store** — large blobs with metadata
- **Subjects with wildcards** — `facts.service.api.auth.*` is pattern matching on tuple structure
- **Persistence** — survives agent death
- **Pub/Sub** — `out` is publish, `in` is queue-group consume (destructive), `rd` is regular subscribe (non-destructive)

The main gap: NATS doesn't do **associative matching** on tuple content (only subject hierarchy). True Linda matching would need a thin layer on top.

### What PostgreSQL + pgvector Gives Us

For L3, we already have Postgres in production:
- **pgvector** — semantic similarity search (the semantic graph)
- **Regular tables** — temporal + entity edges with foreign keys
- **JSONB** — structured attributes on memory nodes
- **Recursive CTEs** — graph traversal for causal chains

The causal graph requires LLM inference (MAGMA's slow path). This can run as a background worker triggered by job completion events.

---

## 5. Implementation Phases

### Phase 1: L2 Shared Cache (NATS KV Tuplespace)

Add a tuplespace abstraction over NATS KV that agents can use during execution:

```python
# New MCP tools for agents
@mcp.tool()
async def publish_fact(job_id: str, category: str, key: str, value: str) -> str:
    """Share a discovered fact with other agents in this job."""
    # out() — write to NATS KV under job-scoped key
    await nats_kv.put(f"facts.{job_id}.{category}.{key}", value)

@mcp.tool()
async def query_facts(job_id: str, category: str, key_pattern: str = "*") -> str:
    """Read facts discovered by any agent in this job."""
    # rd() — non-destructive read with wildcard matching
    entries = await nats_kv.keys(f"facts.{job_id}.{category}.{key_pattern}")
    return json.dumps([{"key": k, "value": await nats_kv.get(k)} for k in entries])

@mcp.tool()
async def claim_work(job_id: str, work_type: str) -> str:
    """Atomically claim a unit of work (destructive read)."""
    # in() — consume tuple so no other agent can claim it
    ...
```

Scope: per-job. Facts auto-expire when job reaches terminal state (or archive to L3).

### Phase 2: L3 Knowledge Graph (PostgreSQL Multi-Graph)

New tables:

```sql
-- Memory nodes (MAGMA-inspired)
CREATE TABLE memory_nodes (
    id UUID PRIMARY KEY,
    content TEXT NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    embedding vector(1536),          -- pgvector
    attributes JSONB DEFAULT '{}',
    source_job_id TEXT,
    source_agent_role TEXT,
    project TEXT
);

-- Temporal edges (immutable timeline)
CREATE TABLE memory_edges_temporal (
    from_node UUID REFERENCES memory_nodes(id),
    to_node UUID REFERENCES memory_nodes(id),
    PRIMARY KEY (from_node, to_node)
);

-- Causal edges (inferred by LLM, async)
CREATE TABLE memory_edges_causal (
    from_node UUID REFERENCES memory_nodes(id),
    to_node UUID REFERENCES memory_nodes(id),
    confidence FLOAT DEFAULT 1.0,
    reasoning TEXT,
    PRIMARY KEY (from_node, to_node)
);

-- Entity edges (entity → event)
CREATE TABLE memory_edges_entity (
    entity_id UUID,          -- abstract entity (file, module, pattern)
    entity_type TEXT,        -- 'file', 'module', 'api_endpoint', 'pattern'
    entity_name TEXT,
    node_id UUID REFERENCES memory_nodes(id),
    PRIMARY KEY (entity_id, node_id)
);

-- Semantic edges computed via pgvector similarity
-- (no table needed — computed at query time via cosine distance)
```

Write path:
- **Fast (synaptic):** On job completion, dump L2 facts + tool call history into memory_nodes + temporal edges
- **Slow (consolidation):** Background worker runs LLM over 2-hop neighborhoods to infer causal edges

Read path (injected into agent prompts):
```python
async def get_relevant_memories(project: str, task_description: str, limit: int = 10) -> list[dict]:
    """MAGMA-style retrieval: embed query, find anchors, traverse graphs."""
    query_embedding = await embed(task_description)
    # 1. Semantic anchors via pgvector
    anchors = await db.query("SELECT * FROM memory_nodes WHERE project = $1 ORDER BY embedding <=> $2 LIMIT 5", project, query_embedding)
    # 2. Expand via temporal + causal + entity edges
    # 3. Score and rank
    # 4. Synthesize into context string
```

### Phase 3: A2A Agent Cards for Observability

Each running agent exposes an Agent Card via the MCP server:

```python
@mcp.resource("agent://{agent_id}/card")
async def agent_card(agent_id: str) -> str:
    """A2A-compatible agent card for observing a running agent."""
    agent = await db.get_agent(agent_id)
    task = await db.get_task(agent.task_id) if agent.task_id else None
    subtasks = await db.get_subtasks(task.id) if task else []
    facts = await nats_kv.keys(f"facts.{agent.job_id}.*") if agent.job_id else []

    return json.dumps({
        "name": f"{agent.role}-{agent.id[:8]}",
        "status": agent.status,
        "task": {
            "id": task.id if task else None,
            "title": task.title if task else None,
            "service": task.service if task else None,
            "revision_count": task.revision_count if task else 0,
        },
        "subtasks": {
            "total": len(subtasks),
            "completed": len([s for s in subtasks if s.status == "completed"]),
            "running": len([s for s in subtasks if s.status == "running"]),
        },
        "facts_published": len(facts),
        "cost_usd": agent.cost_usd,
        "turns": agent.num_turns,
    })
```

### Phase 4: Cross-Job Learning Loop

The feedback loop that makes agents smarter over time:

```
Job N completes
     │
     ▼
L2 facts archived to L3 (fast path: temporal + entity edges)
     │
     ▼
Background consolidation worker (slow path)
     │  - reads 2-hop neighborhood around new nodes
     │  - LLM infers causal edges ("auth change caused test failure because...")
     │  - updates causal graph
     │
     ▼
Job N+1 starts on same project
     │
     ▼
Agent prompt builder queries L3:
     │  "What do we know about project X's auth module?"
     │  → MAGMA traversal returns relevant memories
     │  → injected as "## Prior Knowledge" section in system prompt
     │
     ▼
Agent has institutional memory
```

---

## 6. Obsidian / Zettelkasten Model for Agent Memory

### 6.1 Why Obsidian's Model Fits

Obsidian's knowledge graph emerges from three simple primitives:
1. **Notes** — atomic units of knowledge with a title and body
2. **Tags** — `#auth #jwt #api` — categorical metadata enabling filtered views
3. **Wikilinks** — `[[auth-module]]` — explicit bidirectional edges to other notes

The graph view is a **derived artifact** — it's not designed upfront, it emerges from notes linking to each other over time. This is exactly how agent memory should work: agents create memory notes as they discover things, tag them, link them, and the knowledge graph materializes organically.

This is A-MEM's Zettelkasten approach, but with a concrete UX metaphor everyone already understands.

### 6.2 Mapping: Obsidian Note → Agent Memory Node

```
┌─────────────────────────────────────────────────────────────┐
│ Obsidian Note                  Agent Memory Node             │
│ ─────────────                  ──────────────────            │
│                                                              │
│ # Auth Module Refactor         content: "Refactored auth     │
│                                module to use JWT RS256 with   │
│                                refresh token rotation"        │
│                                                              │
│ tags: #auth #jwt #security     tags: ["auth", "jwt",         │
│                                       "security"]            │
│                                                              │
│ [[api-service]]                entity_links: ["api-service"] │
│ [[previous-review-feedback]]   temporal_links: ["node_abc"]  │
│                                                              │
│ "Changed because the spec      causal_links: ["spec_node_x"] │
│  required SSO integration"     (inferred or explicit)        │
│                                                              │
│ Created: 2026-03-14            timestamp: 2026-03-14T...     │
│ Modified: 2026-03-14           source_job_id: "job_abc"      │
│                                source_agent_role: "backend_  │
│                                  engineer"                   │
│                                project: "export-delivery-    │
│                                  tracker"                    │
└─────────────────────────────────────────────────────────────┘
```

### 6.3 Tag Taxonomy for Agent Memory

Tags serve as the primary **filtering mechanism** — like Obsidian's tag pane. A structured tag taxonomy makes cross-job queries powerful:

```
# Domain tags (what area of the codebase)
#auth  #database  #api  #frontend  #ci  #deploy  #testing

# Action tags (what happened)
#discovery    — agent learned a fact about the codebase
#decision     — agent made a design choice
#bug          — agent found a bug
#constraint   — agent discovered a constraint (schema, API contract, etc.)
#pattern      — agent noticed a recurring pattern
#feedback     — reviewer feedback on agent's work
#regression   — something broke that used to work

# Outcome tags (how things resolved)
#approved     — reviewer approved
#rejected     — reviewer requested changes
#merged       — PR merged
#failed       — task failed
#workaround   — agent used a workaround (tech debt signal)
```

An agent creating a memory note:

```python
@mcp.tool()
async def create_memory_note(
    content: str,
    tags: list[str],
    links: list[str] | None = None,
    job_id: str | None = None,
) -> str:
    """Create a tagged memory note (Zettelkasten-style).

    Tags enable filtered retrieval. Links create explicit graph edges
    to other notes or entities. The knowledge graph emerges from the
    accumulated links across all notes.

    Examples:
      create_memory_note(
        content="users table has a UNIQUE constraint on email column",
        tags=["database", "constraint", "discovery"],
        links=["users-table", "api-service"]
      )
      create_memory_note(
        content="Reviewer flagged: auth middleware doesn't handle token expiry gracefully",
        tags=["auth", "feedback", "bug"],
        links=["auth-module", "review-job-xyz"]
      )
    """
```

### 6.4 Backtrace: The Killer Feature

Obsidian's backlinks panel shows "what links here" — every note that references the current note. For agent memory, this enables **causal backtrace**:

```
Query: "Why was the auth module changed in job def456?"

Backtrace traversal:

  ┌──────────────────┐
  │ auth-module       │ ◄── entity node
  │ (entity)          │
  └────────┬─────────┘
           │ entity edges ("what touched this?")
           │
    ┌──────┴──────┬─────────────────┐
    ▼             ▼                 ▼
  ┌────────┐  ┌────────────┐  ┌──────────────┐
  │ note_1 │  │ note_2     │  │ note_3       │
  │ job:abc│  │ job:def456 │  │ job:def456   │
  │ #disc  │  │ #decision  │  │ #feedback    │
  │ "uses  │  │ "refactored│  │ "reviewer:   │
  │  JWT"  │  │  to RS256" │  │  handle      │
  │        │  │            │  │  expiry"     │
  └────────┘  └──────┬─────┘  └──────────────┘
                     │ causal edge ("why?")
                     ▼
              ┌──────────────┐
              │ note_4       │
              │ job:def456   │
              │ #constraint  │
              │ "spec says   │
              │  SSO required│
              │  for Q2"     │
              └──────┬───────┘
                     │ causal edge
                     ▼
              ┌──────────────┐
              │ spec_node    │
              │ (original    │
              │  job spec)   │
              └──────────────┘

Answer: "The auth module was refactored to JWT RS256 (note_2)
         because the spec required SSO for Q2 (note_4),
         which traces back to the original job specification.
         Note: a prior job (abc) already documented that the
         module used JWT (note_1). The reviewer also flagged
         that token expiry handling needs work (note_3)."
```

This backtrace works because:
1. **Entity edges** find all notes that touched `auth-module`
2. **Causal edges** follow the "why" chain backward
3. **Temporal edges** establish ordering
4. **Tags** filter by relevance (`#decision` and `#constraint` are more relevant to "why" than `#discovery`)

### 6.5 Obsidian-Style Graph Views as Agent Context

When building an agent's prompt, we can generate an "Obsidian graph view" as structured context:

```python
async def build_knowledge_context(project: str, task: Task, max_tokens: int = 2000) -> str:
    """Build an Obsidian-style knowledge context for an agent's prompt.

    Retrieves relevant memory notes, formats them with tags and links,
    and includes a mini-graph showing relationships.
    """
    # 1. Find relevant notes via MAGMA traversal
    notes = await get_relevant_memories(project, task.description)

    # 2. Format as Obsidian-style context
    context_parts = ["## Prior Knowledge (from previous jobs)\n"]

    for note in notes:
        tags_str = " ".join(f"#{t}" for t in note.tags)
        links_str = ", ".join(f"[[{l}]]" for l in note.links) if note.links else ""
        context_parts.append(
            f"### {note.title}\n"
            f"**Tags:** {tags_str}\n"
            f"**Links:** {links_str}\n"
            f"**Source:** Job {note.source_job_id[:8]}, {note.source_agent_role}\n"
            f"{note.content}\n"
        )

    # 3. Add graph summary (like Obsidian's local graph)
    context_parts.append("\n### Knowledge Graph (local view)\n```")
    context_parts.append(format_local_graph(notes))
    context_parts.append("```\n")

    return "\n".join(context_parts)
```

The agent sees something like:

```markdown
## Prior Knowledge (from previous jobs)

### Database Schema Constraint
**Tags:** #database #constraint #discovery
**Links:** [[users-table]], [[api-service]]
**Source:** Job abc12345, backend_engineer
The `users` table has a UNIQUE constraint on the `email` column.
Any upsert logic must handle conflict on email.

### Auth Module Architecture
**Tags:** #auth #discovery #pattern
**Links:** [[auth-module]], [[api-service]]
**Source:** Job abc12345, backend_engineer
Auth uses JWT RS256 with 15-minute access tokens and 7-day refresh tokens.
Middleware in `src/middleware/auth.py` validates on every request.

### Reviewer Feedback on Error Handling
**Tags:** #auth #feedback #bug
**Links:** [[auth-module]]
**Source:** Job def45678, code_reviewer
Token expiry returns 500 instead of 401. The middleware catches
`jwt.ExpiredSignatureError` but re-raises it as a generic exception.

### Knowledge Graph (local view)
auth-module ──── "uses JWT RS256" (job:abc)
     │
     ├──── "reviewer: handle expiry" (job:def) #feedback
     │
     └──── api-service ──── users-table
                              │
                              └── "email UNIQUE constraint" (job:abc) #constraint
```

### 6.6 Agent-Driven Graph Growth

The key Zettelkasten insight: **the author (agent) decides what's noteworthy**. Not every tool call becomes a memory node. The agent uses judgment:

```python
# In the agent's tool definitions, add:

@mcp.tool()
async def create_memory_note(content: str, tags: list[str], links: list[str] | None = None) -> str:
    """Record a noteworthy discovery, decision, or observation for future reference.

    Use this when you learn something that would be valuable to know in future jobs
    on this project. Don't record routine operations — only insights, constraints,
    patterns, decisions, and feedback that affect how this codebase should be worked on.

    Good examples:
    - "The deploy pipeline requires manual approval for production" #deploy #constraint
    - "Tests in /integration/ take 8+ minutes, run unit tests first" #testing #pattern
    - "The payments module has no test coverage for refund flows" #testing #bug #payments

    Bad examples (don't record these):
    - "Read file src/main.py" (routine operation)
    - "Ran git status" (routine operation)
    """
```

Over time, the knowledge graph grows organically — like an Obsidian vault that gets richer with each note. Early jobs produce sparse graphs. After 50 jobs on the same project, agents have deep institutional knowledge.

### 6.7 Revised L3 Schema (Tag-Native)

Update the PostgreSQL schema to make tags first-class:

```sql
-- Memory nodes with tags as a first-class array
CREATE TABLE memory_nodes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    content TEXT NOT NULL,
    title TEXT,                           -- short summary (like Obsidian note title)
    tags TEXT[] DEFAULT '{}',             -- #auth #jwt #constraint
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    embedding vector(1536),
    attributes JSONB DEFAULT '{}',
    source_job_id TEXT,
    source_agent_role TEXT,
    project TEXT NOT NULL
);

-- GIN index for fast tag queries
CREATE INDEX idx_memory_tags ON memory_nodes USING GIN (tags);

-- pgvector index for semantic search
CREATE INDEX idx_memory_embedding ON memory_nodes USING ivfflat (embedding vector_cosine_ops);

-- Explicit links (wikilinks / backlinks)
-- Bidirectional: if A links to B, querying B shows A as a backlink
CREATE TABLE memory_links (
    from_node UUID REFERENCES memory_nodes(id) ON DELETE CASCADE,
    to_entity TEXT NOT NULL,              -- entity name or node ID
    link_type TEXT DEFAULT 'reference',   -- reference, causal, temporal, entity
    created_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (from_node, to_entity, link_type)
);

-- Entity registry (like Obsidian's unresolved links becoming real notes)
CREATE TABLE memory_entities (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT UNIQUE NOT NULL,            -- "auth-module", "users-table", "api-service"
    entity_type TEXT,                     -- file, module, service, pattern, concept
    project TEXT NOT NULL,
    first_seen TIMESTAMPTZ DEFAULT NOW(),
    attributes JSONB DEFAULT '{}'
);
```

Queries that feel like Obsidian:

```sql
-- "Show me everything tagged #auth" (tag pane)
SELECT * FROM memory_nodes WHERE project = 'export-delivery-tracker' AND 'auth' = ANY(tags);

-- "What links to auth-module?" (backlinks panel)
SELECT n.* FROM memory_nodes n
JOIN memory_links l ON l.from_node = n.id
WHERE l.to_entity = 'auth-module';

-- "Show me recent #constraint discoveries" (filtered search)
SELECT * FROM memory_nodes
WHERE project = 'export-delivery-tracker'
  AND 'constraint' = ANY(tags)
ORDER BY timestamp DESC LIMIT 10;

-- "Semantic search: what do we know about token expiry?" (graph + embeddings)
SELECT *, embedding <=> $1 AS distance
FROM memory_nodes
WHERE project = 'export-delivery-tracker'
ORDER BY distance LIMIT 5;
```

---

## 7. Infrastructure: Specialist Tools Per Concern

### 7.1 The Problem with NATS-for-Everything

NATS JetStream is currently asked to do four jobs: messaging, shared cache (KV), blob storage (Object Store), and coordination primitives. It's purpose-built for #1. The rest are "well, streams can also do KV" add-ons with real limitations:

- **No associative matching on values** — NATS KV matches on subject hierarchy (keys), not tuple content
- **No atomic read-and-delete** — can't implement Linda `in()` without a race condition (no `SETNX` equivalent)
- **No per-key TTL** — requires manual purge logic
- **No vector search** — can't do semantic queries over the shared cache
- **No complex queries** — can't filter facts by multiple attributes simultaneously

### 7.2 Recommended Stack: Best Tool Per Concern

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          INFRASTRUCTURE                                 │
│                                                                         │
│  ┌─────────────┐  ┌──────────────┐  ┌────────────────────────────────┐ │
│  │    NATS      │  │    Redis     │  │   PostgreSQL                   │ │
│  │              │  │    Stack     │  │   + AGE + pgvector             │ │
│  │  Messaging   │  │              │  │                                │ │
│  │  only:       │  │  L2 cache:   │  │  L3 knowledge graph:          │ │
│  │  • pub/sub   │  │  • fast KV   │  │  • graph traversal (Cypher)   │ │
│  │  • req/reply │  │  • TTL/expiry│  │  • vector similarity search   │ │
│  │  • agent     │  │  • pub/sub   │  │  • SQL + Cypher in same tx    │ │
│  │    status    │  │    on keys   │  │  • ACID transactions          │ │
│  │  • heartbeat │  │  • JSON docs │  │  • joins with jobs/tasks/     │ │
│  │  • arbiter   │  │  • vector    │  │    agents tables              │ │
│  │    routing   │  │    search    │  │  • existing infra (no new     │ │
│  │              │  │  • Lua       │  │    service)                   │ │
│  │              │  │    scripting │  │                                │ │
│  └─────────────┘  └──────────────┘  └────────────────────────────────┘ │
│                                                                         │
│       messaging        shared state        persistent knowledge         │
│       (keep as-is)     (new service)       (extend existing)            │
└─────────────────────────────────────────────────────────────────────────┘
```

### 7.3 Redis Stack for L2 (Shared Cache / Tuplespace)

Redis Stack bundles RediSearch + RedisJSON into the core Redis server. This combination unlocks real tuplespace semantics:

**Why Redis beats NATS KV for the tuplespace role:**

| Capability | NATS KV | Redis Stack |
|-----------|---------|-------------|
| Fast KV read/write | Yes | Yes (faster — in-memory, single-digit μs) |
| TTL / auto-expiry | Manual purge | Native per-key TTL |
| Pattern match on keys | Subject wildcards | `SCAN` with glob patterns |
| Pattern match on VALUES | No | RediSearch + JSON path queries |
| Atomic operations | No `SETNX` | `SETNX`, `WATCH/MULTI`, Lua scripts |
| Pub/sub on key changes | KV watch | Keyspace notifications |
| JSON document support | Strings only | Native JSON with path queries |
| Vector search | No | HNSW/FLAT with hybrid filtering |
| Sorted sets | No | Native (useful for ranked fact retrieval) |
| Lua scripting | No | Yes — critical for `in()` destructive read |

**Key wins for tuplespace semantics:**

1. **Lua scripting enables true Linda `in()` semantics** — atomic read-and-delete in one round trip:
```lua
-- Atomic in(): search for matching tuple, read it, delete it
local results = redis.call('FT.SEARCH', KEYS[1], ARGV[1], 'LIMIT', 0, 1)
if results[1] == 0 then return nil end
local key = results[2]
local val = redis.call('JSON.GET', key)
redis.call('DEL', key)
return val
```

2. **RediSearch on JSON enables associative matching** — query tuple *content*, not just keys:
```
FT.SEARCH facts "@category:{auth} @confidence:[0.8 +inf]"
```
This is true Linda-style pattern matching: "find a tuple where field[1]='auth' and field[3] > 0.8".

3. **Per-key TTL** — L2 facts auto-expire when job completes. No manual cleanup.

4. **Built-in vector search** — agents can do semantic queries over the shared cache ("what do other agents know about auth?") without hitting L3.

**Tuplespace abstraction over Redis:**

```python
class TupleSpace:
    """Linda tuplespace over Redis Stack."""

    def __init__(self, redis: Redis, namespace: str):
        self.redis = redis
        self.ns = namespace  # e.g., "job:{job_id}"

    async def out(self, *fields, tags: list[str] = None, ttl: int = 3600) -> str:
        """Produce a tuple into the space (Linda out)."""
        key = f"{self.ns}:{uuid4().hex[:8]}"
        doc = {"fields": list(fields), "tags": tags or [], "ts": time.time()}
        await self.redis.json().set(key, "$", doc)
        if ttl:
            await self.redis.expire(key, ttl)
        return key

    async def rd(self, *pattern) -> dict | None:
        """Non-destructive read with pattern matching (Linda rd)."""
        query = self._build_query(pattern)
        results = await self.redis.ft(self.ns).search(query)
        return results[0] if results else None

    async def in_(self, *pattern) -> dict | None:
        """Atomic destructive read (Linda in) — match and consume."""
        return await self.redis.eval(self._IN_SCRIPT, keys=[self.ns], args=[pattern])

    async def watch(self, *pattern, callback: Callable):
        """Subscribe to new tuples matching pattern (reactive rd)."""
        pubsub = self.redis.pubsub()
        await pubsub.psubscribe(f"__keyspace@0__:{self.ns}:*")
        async for msg in pubsub.listen():
            if msg["type"] == "pmessage" and msg["data"] == "json.set":
                key = msg["channel"].split(":", 1)[1]
                doc = await self.redis.json().get(key)
                if self._matches(doc, pattern):
                    await callback(doc)
```

### 7.4 PostgreSQL + Apache AGE + pgvector for L3 (Knowledge Graph)

Instead of raw recursive CTEs for graph traversal, Apache AGE adds Cypher query support as a Postgres extension. Combined with pgvector for embeddings, this gives a full knowledge graph inside the existing Postgres instance.

**Why Apache AGE over Neo4j:**
- **No new service** — it's a Postgres extension, runs in the existing container
- **Unified transactions** — graph writes and regular table writes (jobs, tasks, agents) in the same ACID transaction
- **SQL + Cypher** — can join graph queries with existing application tables
- **Team already knows Postgres** — no new operational knowledge required

**Why AGE over raw recursive CTEs:**
```sql
-- Recursive CTE for 3-hop causal backtrace (hard to read, hard to maintain):
WITH RECURSIVE causal_chain AS (
    SELECT from_node, to_node, 1 AS depth, reasoning
    FROM memory_edges_causal WHERE to_node = $1
    UNION ALL
    SELECT e.from_node, e.to_node, cc.depth + 1, e.reasoning
    FROM memory_edges_causal e
    JOIN causal_chain cc ON e.to_node = cc.from_node
    WHERE cc.depth < 3
)
SELECT n.*, cc.depth, cc.reasoning
FROM causal_chain cc
JOIN memory_nodes n ON n.id = cc.from_node;

-- Equivalent Cypher via AGE (readable, composable):
SELECT * FROM cypher('knowledge', $$
    MATCH (n:Note)-[:CAUSED*1..3]->(target:Note {id: $target_id})
    RETURN n, relationships(n) AS chain
$$) AS (n agtype, chain agtype);
```

**Hybrid query — Cypher graph traversal + pgvector similarity in one query:**
```sql
-- "Find notes causally related to auth-module that are semantically
--  similar to 'token expiry handling'"
SELECT n.content, n.tags, n.embedding <=> $query_embedding AS distance
FROM cypher('knowledge', $$
    MATCH (e:Entity {name: 'auth-module'})<-[:LINKS_TO]-(n:Note)
    RETURN n.id AS node_id
$$) AS (node_id agtype)
JOIN memory_nodes n ON n.id = node_id::text::uuid
ORDER BY n.embedding <=> $query_embedding
LIMIT 5;
```

**Docker Compose addition (AGE + pgvector):**
```yaml
postgres:
    image: apache/age:PG17_latest   # AGE pre-installed on Postgres 17
    environment:
      POSTGRES_USER: minion
      POSTGRES_PASSWORD: minion
      POSTGRES_DB: minion
    volumes:
      - pgdata:/var/lib/postgresql/data
      - ./db/init.sql:/docker-entrypoint-initdb.d/init.sql:ro
    # ...
```

```sql
-- Add to db/init.sql
CREATE EXTENSION IF NOT EXISTS age;
CREATE EXTENSION IF NOT EXISTS vector;
LOAD 'age';
SET search_path = ag_catalog, "$user", public;
SELECT create_graph('knowledge');
```

### 7.5 NATS Stays for Messaging (Unchanged)

NATS keeps its current role — the things it's actually built for:
- Agent status pub/sub (`agents.status.>`)
- Arbiter state transition request/reply (`arbiter.state.transition`)
- Heartbeat routing (`arbiter.heartbeat`)
- K8s Job result messages (`agents.results.>`)
- System event notifications

No changes needed. Just stop asking it to be a cache.

### 7.6 The Wild Card: SurrealDB 3.0

SurrealDB 3.0 (GA February 2026, $23M Series A) is a multi-model database that unifies documents, graphs, vectors, time-series, and KV in one system with one query language. It explicitly markets itself as "the multi-model database for AI agents" with native MCP support for agent memory.

| Capability | Postgres + AGE + pgvector | Redis Stack | SurrealDB 3.0 |
|-----------|--------------------------|-------------|---------------|
| Document store | JSONB | RedisJSON | Native (schemaless) |
| Graph database | AGE (Cypher) | No | Native (graph relations) |
| Vector search | pgvector | RediSearch vectors | Native (HNSW) |
| Time-series | Regular tables | Sorted sets | Native |
| Real-time subscriptions | LISTEN/NOTIFY | Keyspace notifications | `LIVE SELECT` |
| ACID transactions | Yes | Lua scripts | Yes |
| MCP integration | No (custom) | No (custom) | Yes (built-in agent memory) |

**SurrealDB could replace both Redis and Postgres+AGE+pgvector** with a single service. The `LIVE SELECT` feature provides real-time subscriptions that could replace Redis keyspace notifications for L2 cache watching.

**Trade-offs:**
- (+) One database for L2 AND L3 — simpler ops
- (+) Native MCP support — agents can use it as a memory layer out of the box
- (+) Graph + vector + document in one query language
- (-) Young — 3.0 just shipped, smaller ecosystem than Postgres
- (-) Migration risk — moving away from battle-tested Postgres
- (-) Less community knowledge, fewer StackOverflow answers
- (-) Unknown performance at scale compared to Redis (in-memory) and Postgres (decades of optimization)

**Recommendation:** Evaluate in 6 months once 3.0 has production mileage. For now, Redis + Postgres+AGE+pgvector is the safer path with proven components.

### 7.7 Comparison Matrix: All Options

| Concern | Option A (Recommended) | Option B | Option C |
|---------|----------------------|----------|----------|
| **Messaging** | NATS JetStream (keep) | — | — |
| **L2 Shared Cache** | Redis Stack | NATS KV (current) | SurrealDB |
| **L3 Graph Traversal** | Postgres + AGE | Neo4j (new service) | SurrealDB |
| **L3 Vector Search** | Postgres + pgvector | Qdrant (new service) | SurrealDB |
| **L3 Structured Data** | Postgres (existing tables) | — | SurrealDB |
| **Tuplespace Abstraction** | Thin Python layer over Redis | Thin layer over NATS KV | Native SurrealDB queries |

**Option A** adds one new service (Redis) and two Postgres extensions (AGE, pgvector). Total: 4 services (app, Postgres, NATS, Redis).

**Option B** keeps current infra but adds Neo4j and/or Qdrant. Total: 5-6 services. More operational burden, but best-in-class for each concern.

**Option C** replaces Postgres+Redis with SurrealDB. Total: 3 services (app, SurrealDB, NATS). Simplest ops, highest risk.

### 7.8 Updated Docker Compose (Option A)

```yaml
services:
  minion-suite:
    build: .
    image: minion-suite:dev
    env_file: .env
    ports:
      - "${MCP_PORT:-8321}:8321"
    volumes:
      - ./projects.yaml:/app/projects.yaml:ro
      - ./prompts:/app/prompts:ro
      - logs:/app/logs
    depends_on:
      postgres:
        condition: service_healthy
      nats:
        condition: service_started
      redis:
        condition: service_healthy
    restart: unless-stopped

  postgres:
    image: apache/age:PG17_latest        # AGE pre-installed
    environment:
      POSTGRES_USER: minion
      POSTGRES_PASSWORD: minion
      POSTGRES_DB: minion
    volumes:
      - pgdata:/var/lib/postgresql/data
      - ./db/init.sql:/docker-entrypoint-initdb.d/init.sql:ro
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U minion -d minion"]
      interval: 5s
      timeout: 5s
      retries: 10
    ports:
      - "5434:5432"

  nats:
    image: nats:latest
    command: ["-js", "--store_dir=/data"]
    volumes:
      - natsdata:/data
    ports:
      - "4222:4222"
      - "8222:8222"

  redis:
    image: redis/redis-stack:latest       # RediSearch + RedisJSON included
    ports:
      - "6379:6379"
      - "8001:8001"                       # RedisInsight UI (free)
    volumes:
      - redisdata:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 10
    command: >
      redis-stack-server
      --appendonly yes
      --maxmemory 512mb
      --maxmemory-policy allkeys-lru

volumes:
  pgdata:
  natsdata:
  redisdata:
  logs:
```

### 7.9 Updated Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────────┐
│                              Agent                                       │
│                                                                          │
│  ┌──────────────────────────────────────────────────────────────────┐   │
│  │ L1: Context Window (in-process, dies with agent)                 │   │
│  │ • LLM messages, KV cache, current tool results                   │   │
│  └───────────────┬──────────────────────────────────┬───────────────┘   │
│                  │ out()/rd()/in()                   │ query_memories()  │
│                  ▼                                   ▼                   │
│  ┌──────────────────────────┐    ┌──────────────────────────────────┐   │
│  │ L2: Redis Stack           │    │ L3: PostgreSQL + AGE + pgvector  │   │
│  │                           │    │                                  │   │
│  │ Per-job shared cache      │    │ Cross-job knowledge graph        │   │
│  │ • JSON docs with search   │    │ • Cypher graph traversal (AGE)   │   │
│  │ • Tuplespace primitives   │    │ • Vector similarity (pgvector)   │   │
│  │   (Lua atomic ops)        │    │ • SQL joins with app tables      │   │
│  │ • Per-key TTL             │    │ • ACID transactions              │   │
│  │ • Vector search (small)   │    │ • Temporal/causal/entity/        │   │
│  │ • Keyspace notifications  │    │   semantic edges                 │   │
│  │ • RedisInsight UI (:8001) │    │ • Tag-native (GIN index)         │   │
│  │                           │    │ • Obsidian-style backlinks        │   │
│  │ Latency: <1ms             │    │                                  │   │
│  │ Lifetime: job duration     │    │ Latency: 5-50ms                  │   │
│  └──────────┬────────────────┘    │ Lifetime: forever                │   │
│             │ archive on           └──────────────────────────────────┘   │
│             │ job complete                          ▲                     │
│             └──────────────────────────────────────┘                     │
│                    (fast path: temporal + entity edges)                   │
│                    (slow path: LLM-inferred causal edges)                │
│                                                                          │
│  ┌──────────────────────────┐                                           │
│  │ NATS JetStream            │                                           │
│  │                           │                                           │
│  │ Messaging only:           │                                           │
│  │ • Agent status pub/sub    │                                           │
│  │ • Arbiter state transitions│                                          │
│  │ • Heartbeat routing       │                                           │
│  │ • K8s job result messages │                                           │
│  └──────────────────────────┘                                           │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 8. Open Questions

1. **Eviction policy for L2:** LRU? Relevance-scored? Or just TTL per job? The paper says "semantic importance" should factor in, not just recency.  
 - Response: Start with LRU. However, should be configurable to include semantic importance as well. Rationale, for smaller orgs/teams, LRU is sufficient. For large orgs where this could be deployed, 
  semantic relevance is important along with LRU. We should create lru-relevance function for eviction.
 - Follow-up question: does eviction mean that the arbiter will take the eviction data and place it L3 storage?

2. **Causal inference cost:** MAGMA's slow path uses an LLM to infer causal edges. How much does this cost per job? Can we batch consolidation across multiple jobs?
 - Response: Since this is eventual consistency, we can use batch based API usage. Anthropic has this idea, and it's what I've used before for web scraping data extraction. Almost like a send -> poll -> retrieve
 - This should use a strong reasoning model so that causal inference is _accurate_. I can see this adding to potential hallucination. We should create a function to detect drift.

3. **Privacy / isolation:** Should agents in different jobs on the same project see each other's L2 cache? Probably not (job isolation), but L3 should be shared.
 - Response: <thinking> ... Actually: _question_ => _why shouldn't_ agents see each other's L2 cache? Like what reason is so absolute that agents shouldn't have that type of visibility?

4. **Conflict resolution:** CCP's monotonic store avoids conflicts by design. But what if two agents discover contradictory facts? ("API uses JWT" vs "API uses session cookies") — need a reconciliation strategy.
 - Response: ha, this is similar to 2 humans obtaining contradictory facts or understanding differently. Granted, this is our source of truth, and not multiple angles/sources. My gut is to have a reconciliation agent (the arbiter?) that reviews facts and guarantees their accuracy.

5. **Token budget for L3 injection:** How much prior knowledge can we inject before it crowds out the actual task? MAGMA's token budgeting approach (salience-based) is relevant here.
 - Response: I'll have to read into this more. My gut reaction is keep it lean for as long as possible, then gradually increase the token budget to a ceiling. I don't know what that ceiling or floor is, though. I'm fine with some educated arbitrary guesses.

6. **Graph DB vs PostgreSQL:** RESOLVED — Apache AGE extension adds Cypher to Postgres. No new service, same ACID guarantees, joins with existing tables. See Section 7.4.

7. **Linda `in` (destructive read) semantics:** RESOLVED — Redis Lua scripting provides atomic search+read+delete. See Section 7.3.

8. **Tag taxonomy governance:** Should tags be free-form (Obsidian-style) or from a controlled vocabulary? Free-form is more flexible but risks fragmentation (#auth vs #authentication vs #authn). Could use LLM normalization on write.
 - Response: start controlled, see how memory is retrieved, then broaden the boundaries with a flexible approach. I'm thinking we do a 2 turn style categorization where the agent creates their tags, then turns again and categorizes. On the second turn, we match to the control, but we also send out what they initially created to see what should be extended. Kind of like a sorted frequency list.

9. **Note quality signal:** Not all agent discoveries are worth remembering. Should there be a review/curation step before notes enter L3? Or trust the agent's judgment and let low-value notes decay via access frequency?
 - Response: Let low-value notes decay. This is similar to how a human grows and learns. Early on, everything is necessary, but as we mature, we can sift through the mess and only intake what matters, allowing low value information decay.

10. **Graph visualization:** Obsidian's graph view is a powerful debugging tool. Should we render the knowledge graph in the dashboard? Could use d3-force or similar in `dashboard.py`.
 - Yea, 100%

11. **Backlink-driven prompt injection:** When an agent is about to modify a file, auto-query backlinks for that file's entity node and inject relevant notes. "Before you touch `src/auth.py`, here's what previous agents learned about it."
 - Yes, 100%
---

## 9. References

### Papers
- Yu et al., "Multi-Agent Memory from a Computer Architecture Perspective" (2026) — https://arxiv.org/abs/2603.10062
- Jiang et al., "MAGMA: Multi-Graph Agentic Memory Architecture" (2026) — https://arxiv.org/abs/2601.03236
- Packer et al., "MemGPT: Towards LLMs as Operating Systems" (2023) — https://arxiv.org/abs/2310.08560
- A-MEM, "Agentic Memory for LLM Agents" (2025) — https://arxiv.org/abs/2502.12110
- Borghoff, Bottoni, Pareschi, "Coordination and Communication Foundations for Agentic AI" (EUROCAST 2026)
- Gelernter, "Generative Communication in Linda" (1985) — ACM TOPLAS 7(1)
- Saraswat & Rinard, "Concurrent Constraint Programming" (1990) — ACM POPL
- Collaborative Memory: Multi-User Memory Sharing in LLM Agents — https://arxiv.org/html/2505.18279v1
- ICLR 2026 MemAgents Workshop — https://openreview.net/pdf?id=U51WxL382H
- Orthogonalized state machine in the hippocampus — https://www.nature.com/articles/s41586-024-08548-w

### Protocols
- Google A2A Protocol — https://github.com/a2aproject/A2A
- A2A Protocol Spec — https://a2a-protocol.org
- A2A v0.3 Upgrade Announcement — https://cloud.google.com/blog/products/ai-machine-learning/agent2agent-protocol-is-getting-an-upgrade

### Infrastructure
- Apache AGE (Cypher on Postgres) — https://age.apache.org
- Apache AGE vs Neo4j — https://dev.to/pawnsapprentice/apache-age-vs-neo4j-battle-of-the-graph-databases-2m4
- Redis Stack (RediSearch + RedisJSON) — https://redis.io/docs/latest/develop/ai/search-and-query/vectors/
- NATS KV vs Redis — https://hoop.dev/blog/what-nats-redis-actually-does-and-when-to-use-it/
- NATS KV SETNX limitation — https://github.com/nats-io/nats-server/discussions/4803
- pgvector vs Qdrant vs Weaviate (2026) — https://www.firecrawl.dev/blog/best-vector-databases
- SurrealDB 3.0 (multi-model, AI-native) — https://surrealdb.com
- SurrealDB $23M raise (Feb 2026) — https://siliconangle.com/2026/02/17/surrealdb-raises-23m-expand-ai-native-multi-model-database/
