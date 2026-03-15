-- migrate:up

CREATE TABLE IF NOT EXISTS minions.memory_nodes (
    id            TEXT PRIMARY KEY,
    content       TEXT NOT NULL,
    title         TEXT,
    tags          TEXT[] NOT NULL DEFAULT '{}',
    embedding     vector(1536),
    attributes    JSONB NOT NULL DEFAULT '{}',
    source_job_id TEXT,
    source_agent_role TEXT,
    project       TEXT NOT NULL,
    access_count  INTEGER NOT NULL DEFAULT 0,
    last_accessed TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS minions.memory_links (
    id          SERIAL PRIMARY KEY,
    from_node   TEXT NOT NULL REFERENCES minions.memory_nodes(id) ON DELETE CASCADE,
    to_entity   TEXT NOT NULL,
    link_type   TEXT NOT NULL,
    confidence  REAL NOT NULL DEFAULT 1.0,
    reasoning   TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS minions.memory_entities (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    entity_type TEXT,
    project     TEXT NOT NULL,
    first_seen  TIMESTAMPTZ NOT NULL DEFAULT now(),
    attributes  JSONB NOT NULL DEFAULT '{}',
    UNIQUE (name, project)
);

-- Indexes for memory_nodes
CREATE INDEX IF NOT EXISTS idx_memory_nodes_project ON minions.memory_nodes (project);
CREATE INDEX IF NOT EXISTS idx_memory_nodes_created_at ON minions.memory_nodes (created_at);
CREATE INDEX IF NOT EXISTS idx_memory_nodes_tags ON minions.memory_nodes USING GIN (tags);
CREATE INDEX IF NOT EXISTS idx_memory_nodes_embedding ON minions.memory_nodes USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- Indexes for memory_links
CREATE INDEX IF NOT EXISTS idx_memory_links_from_node ON minions.memory_links (from_node);
CREATE INDEX IF NOT EXISTS idx_memory_links_to_entity ON minions.memory_links (to_entity);

-- Indexes for memory_entities
CREATE INDEX IF NOT EXISTS idx_memory_entities_project ON minions.memory_entities (project);

-- migrate:down

DROP TABLE IF EXISTS minions.memory_links;
DROP TABLE IF EXISTS minions.memory_nodes;
DROP TABLE IF EXISTS minions.memory_entities;
