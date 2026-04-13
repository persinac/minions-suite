-- 001_initial_schema.sql
-- Agent Memory — initial schema in the minions schema.
-- Idempotent: safe to run against a DB that already has some of these tables.
--
-- Run with:
--   psql $DATABASE_URL -f migrations/001_initial_schema.sql

-- pgvector extension (required for embedding columns)
CREATE EXTENSION IF NOT EXISTS vector;

-- ── memory_nodes ──────────────────────────────────────────────────────────────
-- Curated knowledge nodes: decisions, insights, checkpoints.
-- Written intentionally by agents or humans. Persisted indefinitely.

CREATE TABLE IF NOT EXISTS minions.memory_nodes (
    id              TEXT        PRIMARY KEY,
    content         TEXT        NOT NULL,
    title           TEXT,
    tags            TEXT[]      NOT NULL DEFAULT '{}',
    embedding       vector(1536),          -- pgvector; NULL until embeddings enabled
    attributes      JSONB       NOT NULL DEFAULT '{}',
    source_job_id   TEXT,
    source_agent_role TEXT,
    project         TEXT        NOT NULL,
    access_count    INTEGER     NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_accessed   TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_memory_nodes_project
    ON minions.memory_nodes (project, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_memory_nodes_tags
    ON minions.memory_nodes USING GIN (tags);

-- ── memory_entities ───────────────────────────────────────────────────────────
-- Named entities referenced by nodes (files, modules, repos, concepts).

CREATE TABLE IF NOT EXISTS minions.memory_entities (
    id          TEXT        PRIMARY KEY,
    name        TEXT        NOT NULL,
    entity_type TEXT,
    project     TEXT        NOT NULL,
    first_seen  TIMESTAMPTZ NOT NULL DEFAULT now(),
    attributes  JSONB       NOT NULL DEFAULT '{}',
    UNIQUE (name, project)
);

CREATE INDEX IF NOT EXISTS idx_memory_entities_project
    ON minions.memory_entities (project);

-- ── memory_links ──────────────────────────────────────────────────────────────
-- Edges in the knowledge graph: node → entity references, temporal, causal.

CREATE TABLE IF NOT EXISTS minions.memory_links (
    from_node   TEXT        NOT NULL REFERENCES minions.memory_nodes(id) ON DELETE CASCADE,
    to_entity   TEXT        NOT NULL,
    link_type   TEXT        NOT NULL DEFAULT 'reference',
    confidence  FLOAT       NOT NULL DEFAULT 1.0,
    reasoning   TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (from_node, to_entity, link_type)
);

CREATE INDEX IF NOT EXISTS idx_memory_links_entity
    ON minions.memory_links (to_entity);

-- ── memory_events ─────────────────────────────────────────────────────────────
-- High-volume, machine-generated event log.
-- Written by tmux hooks (session start/stop, tool use, permission waits).
-- Separate from memory_nodes: raw facts vs. curated knowledge.

CREATE TABLE IF NOT EXISTS minions.memory_events (
    id          TEXT        PRIMARY KEY,
    timestamp   TIMESTAMPTZ NOT NULL DEFAULT now(),
    device      TEXT        NOT NULL DEFAULT '',
    project     TEXT        NOT NULL,
    repo        TEXT,
    branch      TEXT,
    agent_slot  TEXT,
    session_id  TEXT,
    event_type  TEXT        NOT NULL,
    payload     JSONB       NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_memory_events_project_time
    ON minions.memory_events (project, timestamp DESC);

CREATE INDEX IF NOT EXISTS idx_memory_events_type
    ON minions.memory_events (event_type, timestamp DESC);

CREATE INDEX IF NOT EXISTS idx_memory_events_session
    ON minions.memory_events (session_id)
    WHERE session_id IS NOT NULL;
