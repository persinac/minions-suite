-- migrate:up

-- At most one LIVE agent per task, enforced by the database.
--
-- `find_claimable_work` decides ownership in Python -- "does this task have an
-- agent in starting/running?" -- and the caller then creates the row. Between
-- those two steps a second worker runs the same check, sees the same answer,
-- and both win the claim. The window is small and it is hit: the duplicate
-- herder spawn is a recurring collision, and no tighter check inside that
-- function can close it, because the race is in the database.
--
-- NOT a unique index on (task_id, status), which is what the note in
-- minions/server/mcp.py used to prescribe. A task legitimately accumulates
-- SEVERAL agent rows over its life -- `run_engineer` creates a fresh one for
-- the first attempt, for every retry, and for every revision round -- and they
-- all settle into the same terminal status. A pair constraint would reject the
-- second revision's completion: it would break the normal path in order to
-- close a race on the unusual one.
--
-- What is actually invariant is that at most one of those rows is live at a
-- time, so the constraint is partial: unique on task_id alone, restricted to
-- exactly the status set `get_running_agents` and `find_claimable_work`
-- already treat as live. Keep the three in step if that set ever changes.
--
-- `task_id` is nullable and Postgres treats NULLs as distinct in a unique
-- index, so agents with no task (spec analyst, arbiter) are unaffected.

-- Pre-existing duplicates would abort CREATE UNIQUE INDEX and take the deploy
-- with it, so settle them first: keep the newest live claim per task and mark
-- the rest failed. This is the same idiom startup recovery uses for a row it
-- can no longer account for. The row comparison gives a total order, so
-- exactly one survivor remains even when two rows share a started_at.
UPDATE minions.agents a
   SET status = 'failed',
       finished_at = NOW(),
       error = COALESCE(a.error, 'superseded by a later claim on the same task')
 WHERE a.status IN ('starting', 'running')
   AND a.task_id IS NOT NULL
   AND EXISTS (
       SELECT 1
         FROM minions.agents b
        WHERE b.task_id = a.task_id
          AND b.status IN ('starting', 'running')
          AND (b.started_at, b.id) > (a.started_at, a.id)
   );

CREATE UNIQUE INDEX IF NOT EXISTS idx_agents_one_live_per_task
    ON minions.agents (task_id)
 WHERE status IN ('starting', 'running');

-- migrate:down
DROP INDEX IF EXISTS minions.idx_agents_one_live_per_task;
