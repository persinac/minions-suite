-- migrate:up
ALTER TABLE minions.jobs RENAME COLUMN trello_card_id TO external_id;
ALTER INDEX minions.idx_jobs_trello RENAME TO idx_jobs_external_id;

-- migrate:down
ALTER TABLE minions.jobs RENAME COLUMN external_id TO trello_card_id;
ALTER INDEX minions.idx_jobs_external_id RENAME TO idx_jobs_trello;
