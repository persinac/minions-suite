-- migrate:up
ALTER TABLE minions.agents ADD COLUMN k8s_job_name TEXT;
CREATE INDEX idx_agents_k8s_job ON minions.agents (k8s_job_name) WHERE k8s_job_name IS NOT NULL;

-- migrate:down
DROP INDEX IF EXISTS minions.idx_agents_k8s_job;
ALTER TABLE minions.agents DROP COLUMN IF EXISTS k8s_job_name;
