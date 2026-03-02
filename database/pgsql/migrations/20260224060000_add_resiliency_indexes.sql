-- migrate:up

-- Index for finding tasks eligible for retry (failed but not exhausted)
CREATE INDEX IF NOT EXISTS idx_tasks_retry
    ON minions.tasks(status, attempt)
    WHERE status = 'failed' AND attempt < max_attempts;

-- Index for finding stuck tasks (non-terminal, ordered by updated_at)
CREATE INDEX IF NOT EXISTS idx_tasks_stuck
    ON minions.tasks(status, updated_at)
    WHERE status NOT IN ('done', 'failed');

-- Add attempt/max_attempts columns if not present (idempotent)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'minions' AND table_name = 'tasks' AND column_name = 'attempt'
    ) THEN
        ALTER TABLE minions.tasks ADD COLUMN attempt INTEGER NOT NULL DEFAULT 1;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'minions' AND table_name = 'tasks' AND column_name = 'max_attempts'
    ) THEN
        ALTER TABLE minions.tasks ADD COLUMN max_attempts INTEGER NOT NULL DEFAULT 3;
    END IF;
END
$$;

-- migrate:down

DROP INDEX IF EXISTS minions.idx_tasks_retry;
DROP INDEX IF EXISTS minions.idx_tasks_stuck;
ALTER TABLE minions.tasks DROP COLUMN IF EXISTS attempt;
ALTER TABLE minions.tasks DROP COLUMN IF EXISTS max_attempts;
