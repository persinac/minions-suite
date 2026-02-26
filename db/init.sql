-- PostgreSQL schema init for minion-suite.
-- Runs automatically on first container start via docker-entrypoint-initdb.d.
-- All tables live under the "reviewer" schema, matching db_postgres.py.

CREATE SCHEMA IF NOT EXISTS reviewer;

CREATE TABLE IF NOT EXISTS reviewer.reviews (
    id              TEXT PRIMARY KEY,
    project         TEXT NOT NULL,
    mr_url          TEXT NOT NULL,
    mr_id           TEXT NOT NULL,
    branch          TEXT,
    title           TEXT,
    author          TEXT,
    status          TEXT NOT NULL DEFAULT 'queued',
    verdict         TEXT,
    summary         TEXT,
    comments_posted INTEGER NOT NULL DEFAULT 0,
    model           TEXT,
    error           TEXT,
    created_at      TEXT NOT NULL,
    started_at      TEXT,
    completed_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_reviews_status  ON reviewer.reviews (status);
CREATE INDEX IF NOT EXISTS idx_reviews_project ON reviewer.reviews (project);

CREATE TABLE IF NOT EXISTS reviewer.agents (
    id            TEXT PRIMARY KEY,
    review_id     TEXT NOT NULL REFERENCES reviewer.reviews (id),
    model         TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'starting',
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd      REAL    NOT NULL DEFAULT 0.0,
    num_turns     INTEGER NOT NULL DEFAULT 0,
    log_file      TEXT,
    error         TEXT
);

CREATE INDEX IF NOT EXISTS idx_agents_review ON reviewer.agents (review_id);

CREATE TABLE IF NOT EXISTS reviewer.review_comments (
    id          TEXT PRIMARY KEY,
    review_id   TEXT NOT NULL REFERENCES reviewer.reviews (id),
    file_path   TEXT NOT NULL,
    line        INTEGER,
    severity    TEXT NOT NULL DEFAULT 'nit',
    body        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_comments_review ON reviewer.review_comments (review_id);
