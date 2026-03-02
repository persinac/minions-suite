-- migrate:up

-- Add review-job fields to jobs table
ALTER TABLE minions.jobs ADD COLUMN IF NOT EXISTS job_type TEXT NOT NULL DEFAULT 'development';
ALTER TABLE minions.jobs ADD COLUMN IF NOT EXISTS mr_url TEXT;

CREATE INDEX IF NOT EXISTS idx_jobs_type ON minions.jobs (job_type);
CREATE INDEX IF NOT EXISTS idx_jobs_mr_url ON minions.jobs (mr_url) WHERE mr_url IS NOT NULL;

-- Add review-task fields to tasks table
ALTER TABLE minions.tasks ADD COLUMN IF NOT EXISTS mr_url TEXT;
ALTER TABLE minions.tasks ADD COLUMN IF NOT EXISTS mr_id TEXT;
ALTER TABLE minions.tasks ADD COLUMN IF NOT EXISTS verdict TEXT;
ALTER TABLE minions.tasks ADD COLUMN IF NOT EXISTS comments_posted INT NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_tasks_mr_url ON minions.tasks (mr_url) WHERE mr_url IS NOT NULL;

-- migrate:down

DROP INDEX IF EXISTS minions.idx_tasks_mr_url;
ALTER TABLE minions.tasks DROP COLUMN IF EXISTS comments_posted;
ALTER TABLE minions.tasks DROP COLUMN IF EXISTS verdict;
ALTER TABLE minions.tasks DROP COLUMN IF EXISTS mr_id;
ALTER TABLE minions.tasks DROP COLUMN IF EXISTS mr_url;

DROP INDEX IF EXISTS minions.idx_jobs_mr_url;
DROP INDEX IF EXISTS minions.idx_jobs_type;
ALTER TABLE minions.jobs DROP COLUMN IF EXISTS mr_url;
ALTER TABLE minions.jobs DROP COLUMN IF EXISTS job_type;
