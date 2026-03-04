-- migrate:up
ALTER TABLE minions.jobs ADD COLUMN IF NOT EXISTS gitlab_issue_id TEXT;
CREATE INDEX IF NOT EXISTS idx_jobs_gitlab_issue
    ON minions.jobs(gitlab_issue_id) WHERE gitlab_issue_id IS NOT NULL;

-- migrate:down
DROP INDEX IF EXISTS minions.idx_jobs_gitlab_issue;
ALTER TABLE minions.jobs DROP COLUMN IF EXISTS gitlab_issue_id;
