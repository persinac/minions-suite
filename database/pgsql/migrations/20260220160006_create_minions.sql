-- migrate:up

CREATE SCHEMA IF NOT EXISTS minions;

-- ============================================================
-- Core tables
-- ============================================================

-- Jobs (formerly "workflows") — top-level unit of work, 1:1 with a Trello card
CREATE TABLE IF NOT EXISTS minions.jobs (
    id              TEXT PRIMARY KEY,              -- 8-char hex UUID
    spec            TEXT NOT NULL,                 -- raw or refined feature spec
    status          TEXT NOT NULL DEFAULT 'spec_received',
    correlation_id  TEXT,                          -- for distributed tracing (Phase 3)
    trello_card_id  TEXT,
    error           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_jobs_status
    ON minions.jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_trello
    ON minions.jobs(trello_card_id)
    WHERE trello_card_id IS NOT NULL;

-- Tasks — individual work items assigned to a specific agent
CREATE TABLE IF NOT EXISTS minions.tasks (
    id              TEXT PRIMARY KEY,              -- 8-char hex UUID
    job_id          TEXT NOT NULL REFERENCES minions.jobs(id),
    title           TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    service         TEXT NOT NULL,                 -- e.g. management-api
    agent_role      TEXT NOT NULL,                 -- AgentRole enum value
    status          TEXT NOT NULL DEFAULT 'pending',
    branch_name     TEXT,
    pr_number       INT,
    pr_url          TEXT,
    review_status   TEXT,                          -- approved, changes_requested, etc.
    deploy_status   TEXT,                          -- success, failed
    revision_count  INT NOT NULL DEFAULT 0,
    timeout_seconds INT NOT NULL DEFAULT 600,      -- per-task timeout (Phase 3)
    claimed_by      TEXT,                          -- agent instance ID (Phase 3)
    attempt         INT NOT NULL DEFAULT 1,        -- current attempt number (Phase 3)
    max_attempts    INT NOT NULL DEFAULT 3,        -- max retry attempts (Phase 3)
    error           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tasks_status_role
    ON minions.tasks(status, agent_role);
CREATE INDEX IF NOT EXISTS idx_tasks_job
    ON minions.tasks(job_id, created_at);

-- Agents — one record per agent subprocess invocation
CREATE TABLE IF NOT EXISTS minions.agents (
    id                      TEXT PRIMARY KEY,      -- 8-char hex UUID
    job_id                  TEXT NOT NULL REFERENCES minions.jobs(id),
    role                    TEXT NOT NULL,          -- AgentRole enum value
    task_id                 TEXT REFERENCES minions.tasks(id),
    pid                     INT,                   -- OS process ID
    status                  TEXT NOT NULL DEFAULT 'starting',
    host                    TEXT,                   -- hostname (for future distribution)
    started_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at             TIMESTAMPTZ,
    error                   TEXT,
    log_file                TEXT,
    input_tokens            INT DEFAULT 0,
    output_tokens           INT DEFAULT 0,
    cache_read_tokens       INT DEFAULT 0,
    cache_creation_tokens   INT DEFAULT 0,
    cost_usd                REAL DEFAULT 0.0,
    num_turns               INT DEFAULT 0,
    model                   TEXT
);

CREATE INDEX IF NOT EXISTS idx_agents_job
    ON minions.agents(job_id, started_at);
CREATE INDEX IF NOT EXISTS idx_agents_status
    ON minions.agents(status);

-- Messages — inter-agent communication (store-and-forward)
CREATE TABLE IF NOT EXISTS minions.messages (
    id          TEXT PRIMARY KEY,                   -- 8-char hex UUID
    job_id      TEXT NOT NULL REFERENCES minions.jobs(id),
    from_role   TEXT NOT NULL,
    to_role     TEXT,                               -- NULL = broadcast
    content     TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_messages_job_role
    ON minions.messages(job_id, to_role);

-- Tool calls — audit log of every MCP tool invocation
CREATE TABLE IF NOT EXISTS minions.tool_calls (
    id          BIGSERIAL PRIMARY KEY,
    job_id      TEXT,
    tool_name   TEXT NOT NULL,
    params      JSONB,
    result      TEXT,                               -- first 1000 chars of result
    error       TEXT,
    duration_ms REAL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tool_calls_job
    ON minions.tool_calls(job_id);
CREATE INDEX IF NOT EXISTS idx_tool_calls_name
    ON minions.tool_calls(tool_name);
CREATE INDEX IF NOT EXISTS idx_tool_calls_created
    ON minions.tool_calls(created_at);

-- Events — system event log for timeline reconstruction
CREATE TABLE IF NOT EXISTS minions.events (
    id          BIGSERIAL PRIMARY KEY,
    job_id      TEXT,
    event_type  TEXT NOT NULL,                     -- e.g. job_created, agent_launched
    source      TEXT,                               -- component that emitted the event
    detail      TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_events_job
    ON minions.events(job_id);
CREATE INDEX IF NOT EXISTS idx_events_type
    ON minions.events(event_type);
CREATE INDEX IF NOT EXISTS idx_events_created
    ON minions.events(created_at);

-- ============================================================
-- Phase 2: NATS message archival
-- ============================================================

-- Message log — durable record of NATS messages for replay and debugging
CREATE TABLE IF NOT EXISTS minions.message_log (
    id              BIGSERIAL PRIMARY KEY,
    nats_subject    TEXT NOT NULL,
    nats_sequence   BIGINT,
    job_id          TEXT,
    from_role       TEXT,
    to_role         TEXT,
    message_type    TEXT NOT NULL,
    payload         JSONB,
    correlation_id  TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_message_log_job
    ON minions.message_log(job_id, created_at);
CREATE INDEX IF NOT EXISTS idx_message_log_correlation
    ON minions.message_log(correlation_id)
    WHERE correlation_id IS NOT NULL;

-- ============================================================
-- Phase 3: Arbiter pattern
-- ============================================================

-- Subtasks — granular breakdown of each task (one subtask = one LLM call)
CREATE TABLE IF NOT EXISTS minions.subtasks (
    id              TEXT PRIMARY KEY,              -- 8-char hex UUID
    task_id         TEXT NOT NULL REFERENCES minions.tasks(id),
    sequence_num    INT NOT NULL,
    description     TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    timeout_seconds INT NOT NULL DEFAULT 120,
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    result          JSONB,
    error           TEXT,
    attempt         INT NOT NULL DEFAULT 1,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_subtasks_task
    ON minions.subtasks(task_id, sequence_num);
CREATE INDEX IF NOT EXISTS idx_subtasks_status
    ON minions.subtasks(status);

-- Heartbeats — agent liveness tracking (high write volume)
CREATE TABLE IF NOT EXISTS minions.heartbeats (
    agent_id            TEXT PRIMARY KEY,
    agent_role          TEXT NOT NULL,
    job_id              TEXT,
    current_task_id     TEXT,
    current_subtask_id  TEXT,
    last_seen           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status              TEXT NOT NULL DEFAULT 'idle'
);

CREATE INDEX IF NOT EXISTS idx_heartbeats_status
    ON minions.heartbeats(status, last_seen);

-- State transitions — audit log of every proposed/approved/rejected transition
CREATE TABLE IF NOT EXISTS minions.state_transitions (
    id                  BIGSERIAL PRIMARY KEY,
    job_id              TEXT,
    task_id             TEXT,
    subtask_id          TEXT,
    agent_id            TEXT,
    from_status         TEXT NOT NULL,
    to_status           TEXT NOT NULL,
    approved            BOOLEAN NOT NULL,
    rejection_reason    TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_transitions_job
    ON minions.state_transitions(job_id, created_at);

-- ============================================================
-- Updated timestamp trigger
-- ============================================================

CREATE OR REPLACE FUNCTION minions.set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER trg_jobs_updated_at
    BEFORE UPDATE ON minions.jobs
    FOR EACH ROW EXECUTE FUNCTION minions.set_updated_at();

CREATE OR REPLACE TRIGGER trg_tasks_updated_at
    BEFORE UPDATE ON minions.tasks
    FOR EACH ROW EXECUTE FUNCTION minions.set_updated_at();

-- migrate:down

-- Drop triggers first
DROP TRIGGER IF EXISTS trg_tasks_updated_at ON minions.tasks;
DROP TRIGGER IF EXISTS trg_jobs_updated_at ON minions.jobs;
DROP FUNCTION IF EXISTS minions.set_updated_at();

-- Drop Phase 3 tables (Arbiter pattern)
DROP TABLE IF EXISTS minions.state_transitions;
DROP TABLE IF EXISTS minions.heartbeats;
DROP TABLE IF EXISTS minions.subtasks;

-- Drop Phase 2 table (NATS archival)
DROP TABLE IF EXISTS minions.message_log;

-- Drop core tables (reverse dependency order)
DROP TABLE IF EXISTS minions.events;
DROP TABLE IF EXISTS minions.tool_calls;
DROP TABLE IF EXISTS minions.messages;
DROP TABLE IF EXISTS minions.agents;
DROP TABLE IF EXISTS minions.tasks;
DROP TABLE IF EXISTS minions.jobs;

-- Drop the schema
DROP SCHEMA IF EXISTS minions;
