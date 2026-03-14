## ADDED Requirements

### Requirement: MemoryStore CRUD operations
The MemoryStore SHALL delegate all CRUD operations to its `MemoryStoreBackend`: `create_node`, `get_node`, `query_by_tags`, `query_by_similarity`, `create_link`, `get_backlinks`, `ensure_entity`, `increment_access`.

#### Scenario: Create and retrieve a node
- **WHEN** calling `store.create_node(node)` with a valid MemoryNode
- **THEN** the node is persisted and retrievable via `store.get_node(node_id)`

#### Scenario: Query returns None for nonexistent node
- **WHEN** calling `store.get_node("nonexistent-id")`
- **THEN** the result is None

### Requirement: Tag-based querying
The MemoryStore SHALL support `query_by_tags(project, tags, limit)` returning nodes matching any of the given tags, scoped to the project.

#### Scenario: Query by single tag
- **WHEN** nodes exist with tags=["auth", "security"] and tags=["infra"] in project "payments-api"
- **WHEN** calling `store.query_by_tags("payments-api", ["auth"])`
- **THEN** only the node tagged "auth" is returned

#### Scenario: Query by multiple tags (OR semantics)
- **WHEN** calling `store.query_by_tags("payments-api", ["auth", "infra"])`
- **THEN** both nodes are returned (union of matches)

### Requirement: Similarity-based querying
The MemoryStore SHALL support `query_by_similarity(project, embedding, limit)` returning nodes ranked by vector distance (cosine or L2) to the provided embedding.

#### Scenario: Similarity search returns ranked results
- **WHEN** 3 nodes exist with embeddings in project "payments-api"
- **WHEN** calling `store.query_by_similarity("payments-api", query_embedding, limit=2)`
- **THEN** the 2 most similar nodes are returned, ordered by distance (closest first)

### Requirement: Entity management
The MemoryStore SHALL support `ensure_entity(name, project, entity_type)` that creates an entity if it doesn't exist (idempotent, deduplication by name+project) and returns its ID.

#### Scenario: Idempotent entity creation
- **WHEN** calling `store.ensure_entity("auth-module", "payments-api", "module")` twice
- **THEN** both calls return the same entity ID and only one entity record exists

### Requirement: Link creation and backlink traversal
The MemoryStore SHALL support `create_link(from_id, to_entity, link_type, confidence, reasoning)` to link a node to a named entity, and `get_backlinks(entity_name, project, limit)` to find all nodes linking to a given entity.

#### Scenario: Create link and traverse backlinks
- **WHEN** a node is created and linked to entity "auth-module" with link_type="mentions"
- **WHEN** calling `store.get_backlinks("auth-module", "payments-api")`
- **THEN** the linked node is returned in the results

#### Scenario: Backlinks scoped to project
- **WHEN** nodes in different projects link to entities with the same name
- **WHEN** calling `store.get_backlinks("auth-module", "payments-api")`
- **THEN** only nodes from "payments-api" are returned

### Requirement: Access count tracking
The MemoryStore SHALL support `increment_access(node_id)` that increments the access_count of a node by 1.

#### Scenario: Increment access
- **WHEN** a node exists with access_count=0
- **WHEN** calling `store.increment_access(node_id)` twice
- **THEN** the node's access_count is 2

### Requirement: Postgres memory backend
The `PostgresMemoryBackend` SHALL implement `MemoryStoreBackend` using psycopg3 async pool with pgvector for similarity search and AGE for graph traversal (falling back to recursive CTEs if AGE is unavailable).

#### Scenario: Similarity search uses pgvector
- **WHEN** querying by similarity
- **THEN** the backend uses `ORDER BY embedding <=> $1 LIMIT $2` (pgvector cosine distance operator)

#### Scenario: Backlinks use JOIN on memory_links
- **WHEN** calling `get_backlinks("auth-module", "payments-api")`
- **THEN** the backend JOINs `memory_nodes` with `memory_links` on `to_entity` and filters by project

### Requirement: MAGMA-style retrieval scoring
The retrieval module SHALL provide `get_relevant_memories(store, project, query_embedding, tags, max_tokens)` that scores memories using a weighted combination of: semantic similarity, tag overlap, recency, access frequency, and link density. Results SHALL be ranked by composite score and truncated to fit within `max_tokens`.

#### Scenario: Scoring combines multiple signals
- **WHEN** two nodes exist — one with high similarity but old, one with lower similarity but recent and highly accessed
- **THEN** the scoring weights determine which ranks higher (configurable weights)

#### Scenario: Token budget enforcement
- **WHEN** 10 relevant memories exist but `max_tokens=500`
- **THEN** only memories that fit within the 500-token budget are returned, in ranked order

### Requirement: File backlink retrieval
The retrieval module SHALL provide `get_file_backlinks(store, project, file_paths, max_tokens)` that returns memories linked to entities matching the given file paths.

#### Scenario: Retrieve memories for changed files
- **WHEN** memories are linked to entity "src/auth/handler.py"
- **WHEN** calling `get_file_backlinks(store, "payments-api", ["src/auth/handler.py"])`
- **THEN** those memories are returned

### Requirement: Access-frequency decay
The decay module SHALL provide `apply_decay(store, project, threshold_days)` that reduces the salience of old, unaccessed nodes.

#### Scenario: Old unaccessed nodes decay
- **WHEN** a node was created 90 days ago with access_count=0 and threshold_days=60
- **WHEN** running `apply_decay`
- **THEN** the node's salience is reduced (lower ranking in future retrievals)

#### Scenario: Frequently accessed nodes resist decay
- **WHEN** a node was created 90 days ago but has access_count=50
- **WHEN** running `apply_decay`
- **THEN** the node's salience is minimally affected due to high access count
