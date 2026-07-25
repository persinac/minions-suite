-- migrate:up
-- Which expert reviewer a task belongs to (api, dba, frontend, ...). NULL means
-- "the single general reviewer", which is every task that predates fan-out.
--
-- Needed before specialist fan-out can work at all: run_task_review dedupes
-- reviewers by pr_url alone, so N specialists on one PR would collapse to one
-- with no error — four silently missing reviews. The dedup key becomes
-- (pr_url, specialty).
ALTER TABLE minions.tasks ADD COLUMN IF NOT EXISTS specialty TEXT;

CREATE INDEX IF NOT EXISTS idx_tasks_pr_specialty
    ON minions.tasks(pr_url, specialty) WHERE pr_url IS NOT NULL;

-- migrate:down
DROP INDEX IF EXISTS minions.idx_tasks_pr_specialty;
ALTER TABLE minions.tasks DROP COLUMN IF EXISTS specialty;
