-- migrate:up

CREATE TABLE IF NOT EXISTS minions.memory_operations (
    id            SERIAL PRIMARY KEY,
    op            TEXT NOT NULL,
    tier          TEXT NOT NULL DEFAULT '',
    project       TEXT NOT NULL DEFAULT '',
    job_id        TEXT NOT NULL DEFAULT '',
    agent_role    TEXT NOT NULL DEFAULT '',
    duration_ms   REAL NOT NULL DEFAULT 0.0,
    details       JSONB NOT NULL DEFAULT '{}',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_memory_operations_created_at ON minions.memory_operations (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_memory_operations_project ON minions.memory_operations (project);

-- migrate:down

DROP TABLE IF EXISTS minions.memory_operations;
