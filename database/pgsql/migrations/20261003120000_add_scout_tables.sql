-- migrate:up
-- The scout station (openspec/changes/factory-stations/, phase 2).
--
-- scout_signals: what the deterministic first phase measured in a repo -- churn
-- hot spots, TODO density, a missing test command. Kept so a finding can be
-- checked against the numbers that prompted it, and so "oldest-scouted first"
-- has a record to read.
--
-- scout_findings: every finding the scout tried to file, filed or refused.
-- The 90-day duplicate check reads `outcome = 'filed'` rows by fingerprint;
-- refusals are kept for the minion_scout_findings_total metric, so a scout
-- whose findings keep bouncing off the contract is visible.
CREATE TABLE IF NOT EXISTS minions.scout_signals (
    id          BIGSERIAL PRIMARY KEY,
    job_id      TEXT NOT NULL,
    repo        TEXT NOT NULL,
    signals     JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_scout_signals_repo
    ON minions.scout_signals (repo, created_at DESC);

CREATE TABLE IF NOT EXISTS minions.scout_findings (
    id           BIGSERIAL PRIMARY KEY,
    job_id       TEXT NOT NULL,
    repo         TEXT NOT NULL,
    kind         TEXT NOT NULL,
    fingerprint  TEXT NOT NULL,
    title        TEXT NOT NULL,
    outcome      TEXT NOT NULL,   -- 'filed', or 'refused_<reason>'
    card_id      TEXT,
    card_url     TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_scout_findings_fingerprint
    ON minions.scout_findings (fingerprint, created_at DESC)
    WHERE outcome = 'filed';

CREATE INDEX IF NOT EXISTS idx_scout_findings_job
    ON minions.scout_findings (job_id);

-- migrate:down
DROP TABLE IF EXISTS minions.scout_findings;
DROP TABLE IF EXISTS minions.scout_signals;
