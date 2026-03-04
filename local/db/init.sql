-- Auto-generated init script for local Docker development.
-- Combines all dbmate migrations (up sections) into a single file
-- that Postgres runs on first boot via docker-entrypoint-initdb.d.
--
-- For production, use dbmate migrations in database/pgsql/migrations/.

-- ============================================================
-- 20260220160006_create_minions.sql
-- ============================================================

CREATE SCHEMA IF NOT EXISTS minions;

-- Jobs — top-level unit of work
CREATE TABLE IF NOT EXISTS minions.jobs (
    id              TEXT PRIMARY KEY,
    spec            TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'spec_received',
    correlation_id  TEXT,
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
    id              TEXT PRIMARY KEY,
    job_id          TEXT NOT NULL REFERENCES minions.jobs(id),
    title           TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    service         TEXT NOT NULL,
    agent_role      TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    branch_name     TEXT,
    pr_number       INT,
    pr_url          TEXT,
    review_status   TEXT,
    deploy_status   TEXT,
    revision_count  INT NOT NULL DEFAULT 0,
    timeout_seconds INT NOT NULL DEFAULT 600,
    claimed_by      TEXT,
    attempt         INT NOT NULL DEFAULT 1,
    max_attempts    INT NOT NULL DEFAULT 3,
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
    id                      TEXT PRIMARY KEY,
    job_id                  TEXT NOT NULL REFERENCES minions.jobs(id),
    role                    TEXT NOT NULL,
    task_id                 TEXT REFERENCES minions.tasks(id),
    pid                     INT,
    status                  TEXT NOT NULL DEFAULT 'starting',
    host                    TEXT,
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

-- Messages — inter-agent communication
CREATE TABLE IF NOT EXISTS minions.messages (
    id          TEXT PRIMARY KEY,
    job_id      TEXT NOT NULL REFERENCES minions.jobs(id),
    from_role   TEXT NOT NULL,
    to_role     TEXT,
    content     TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_messages_job_role
    ON minions.messages(job_id, to_role);

-- Tool calls — audit log
CREATE TABLE IF NOT EXISTS minions.tool_calls (
    id          BIGSERIAL PRIMARY KEY,
    job_id      TEXT,
    tool_name   TEXT NOT NULL,
    params      JSONB,
    result      TEXT,
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

-- Events — system event log
CREATE TABLE IF NOT EXISTS minions.events (
    id          BIGSERIAL PRIMARY KEY,
    job_id      TEXT,
    event_type  TEXT NOT NULL,
    source      TEXT,
    detail      TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_events_job
    ON minions.events(job_id);
CREATE INDEX IF NOT EXISTS idx_events_type
    ON minions.events(event_type);
CREATE INDEX IF NOT EXISTS idx_events_created
    ON minions.events(created_at);

-- NATS message archival
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

-- Subtasks — granular breakdown (Arbiter pattern)
CREATE TABLE IF NOT EXISTS minions.subtasks (
    id              TEXT PRIMARY KEY,
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

-- Heartbeats — agent liveness tracking
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

-- State transitions — audit log
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

-- Updated timestamp trigger
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

-- ============================================================
-- 20260224060000_add_resiliency_indexes.sql
-- ============================================================

CREATE INDEX IF NOT EXISTS idx_tasks_retry
    ON minions.tasks(status, attempt)
    WHERE status = 'failed' AND attempt < max_attempts;

CREATE INDEX IF NOT EXISTS idx_tasks_stuck
    ON minions.tasks(status, updated_at)
    WHERE status NOT IN ('done', 'failed');

-- ============================================================
-- 20260225120000_add_k8s_job_name.sql
-- ============================================================

ALTER TABLE minions.agents ADD COLUMN IF NOT EXISTS k8s_job_name TEXT;
CREATE INDEX IF NOT EXISTS idx_agents_k8s_job
    ON minions.agents(k8s_job_name) WHERE k8s_job_name IS NOT NULL;

-- ============================================================
-- 20260302161020_review_to_job.sql
-- ============================================================

ALTER TABLE minions.jobs ADD COLUMN IF NOT EXISTS job_type TEXT NOT NULL DEFAULT 'development';
ALTER TABLE minions.jobs ADD COLUMN IF NOT EXISTS mr_url TEXT;

CREATE INDEX IF NOT EXISTS idx_jobs_type ON minions.jobs (job_type);
CREATE INDEX IF NOT EXISTS idx_jobs_mr_url ON minions.jobs (mr_url) WHERE mr_url IS NOT NULL;

ALTER TABLE minions.tasks ADD COLUMN IF NOT EXISTS mr_url TEXT;
ALTER TABLE minions.tasks ADD COLUMN IF NOT EXISTS mr_id TEXT;
ALTER TABLE minions.tasks ADD COLUMN IF NOT EXISTS verdict TEXT;
ALTER TABLE minions.tasks ADD COLUMN IF NOT EXISTS comments_posted INT NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_tasks_mr_url ON minions.tasks (mr_url) WHERE mr_url IS NOT NULL;

-- ============================================================
-- 20260303120000_add_gitlab_issue_id.sql
-- ============================================================

ALTER TABLE minions.jobs ADD COLUMN IF NOT EXISTS gitlab_issue_id TEXT;
CREATE INDEX IF NOT EXISTS idx_jobs_gitlab_issue
    ON minions.jobs(gitlab_issue_id) WHERE gitlab_issue_id IS NOT NULL;
