-- Replay corpus for validating a classifier change (see
-- openspec/changes/typesafe-classifier/design.md §5).
--
-- Every classified job already carries its four RICE factors, because
-- minions/engine/dev.py:647 records the fully-rendered reason string:
--
--   R=5 I=1.00 C=0.60 E=6.0 -> E/C=10.00 => hard (new test-fixture pattern)
--
-- Joined to realized cost and agent outcomes, that gives
-- spec -> factors -> tier -> what actually happened, which is what a
-- classifier swap has to be judged against. Production cannot A/B models
-- (routing is deterministic) and task e2e:matrix covers only the analyst and
-- arbiter, so replay against this corpus is the only available oracle.
--
-- VERIFIED against the real schema, NOT against real data. The three up-section
-- migrations above were applied to a scratch database and this query was run
-- over three synthetic rows whose reason strings were produced by calling the
-- actual classify_difficulty() with only litellm.acompletion faked — so the
-- regexes below are checked against the format the code really emits, not
-- against a hand-copied fixture that could drift from it.
--
-- What that run established, and what would have broken it:
--   * `C=` appears TWICE per reason string (`C=0.60` and `E/C=10.00`). Postgres
--     substring() takes the first match, which is the wanted one. Had it not,
--     rice_confidence would read 10.00 and the identity below would fail.
--   * effort / rice_confidence = ec_ratio held on all three rows.
--   * tier extracted from the string matched jobs.difficulty on all three.
--   * the `herder:%` exclusion dropped a $0.00 external row (agent_runs 2, not 3).
--
-- Still unmeasured: corpus SIZE. The deployed database is where real rows live;
-- reach it with the kubectl exec path in docs/operator-setup.md.
--
-- Two things deliberately NOT computed here, because they need a value this
-- query cannot see:
--   * ceiling_hits — an agent that exhausts AGENT_MAX_TURNS is left
--     status='done', error=NULL, so it is only detectable by comparing
--     num_turns against the AGENT_MAX_TURNS in force at the time. Substitute
--     the configured value for :max_turns below.
--   * revision rounds — needs the revision event_type name, which was not
--     verified when this was written. Add it once confirmed.

\set max_turns 32

WITH classified AS (
    SELECT
        e.job_id,
        e.created_at                                            AS classified_at,
        e.detail                                                AS reason,
        -- The reason string is a stable format built in classifier.py, not
        -- free text, so these extractions are safe until that f-string changes.
        (substring(e.detail from 'R=([0-9.]+)'))::numeric        AS reach,
        (substring(e.detail from 'I=([0-9.]+)'))::numeric        AS impact,
        (substring(e.detail from 'C=([0-9.]+)'))::numeric        AS rice_confidence,
        (substring(e.detail from 'E=([0-9.]+)'))::numeric        AS effort,
        (substring(e.detail from 'E/C=([0-9.]+)'))::numeric      AS ec_ratio,
        substring(e.detail from '=> ([a-z]+)')                   AS tier,
        -- Keep only the newest classification per job. A job is classified once
        -- (dev.py guards on `job.difficulty is None`), but a relaunch after a
        -- manual difficulty reset would leave two rows.
        ROW_NUMBER() OVER (PARTITION BY e.job_id ORDER BY e.created_at DESC) AS rn
    FROM minions.events e
    WHERE e.event_type = 'difficulty_classified'
),
outcome AS (
    SELECT
        a.job_id,
        SUM(a.cost_usd)                                                      AS total_cost_usd,
        COUNT(*)                                                             AS agent_runs,
        MAX(a.num_turns)                                                     AS max_turns_used,
        -- real_failed, per the CLAUDE.md rule: a turn-0 death is infrastructure,
        -- not a model or routing failure, and must not be charged to either.
        COUNT(*) FILTER (WHERE a.status = 'failed' AND a.num_turns > 0)       AS real_failed,
        COUNT(*) FILTER (WHERE a.status = 'failed' AND a.num_turns = 0)       AS turn0_failed,
        -- Silent turn-ceiling exhaustion. See the :max_turns note in the header.
        COUNT(*) FILTER (WHERE a.status = 'done' AND a.num_turns >= :max_turns) AS ceiling_hits,
        STRING_AGG(DISTINCT a.model, ',' ORDER BY a.model)                   AS models_used
    FROM minions.agents a
    -- Exclude external subscription-backed workers: they record $0.00 by design
    -- and averaging them in invents a flawless free model.
    WHERE a.model IS NULL OR a.model NOT LIKE 'herder:%'
    GROUP BY a.job_id
)
SELECT
    j.id                                        AS job_id,
    c.classified_at,
    c.tier,
    j.difficulty                                AS tier_persisted,
    c.reach,
    c.impact,
    c.rice_confidence,
    c.effort,
    c.ec_ratio,
    -- The raw ticket is the replay input. original_spec is preserved on first
    -- refine via COALESCE, so it is the pre-analyst text when present; spec is
    -- the refined version and is the fallback.
    COALESCE(j.original_spec, j.spec)            AS replay_state,
    LENGTH(COALESCE(j.original_spec, j.spec))    AS state_chars,
    j.status                                     AS job_status,
    j.error                                      AS job_error,
    o.total_cost_usd,
    o.agent_runs,
    o.max_turns_used,
    o.real_failed,
    o.turn0_failed,
    o.ceiling_hits,
    o.models_used
FROM classified c
JOIN minions.jobs j  ON j.id = c.job_id
LEFT JOIN outcome o  ON o.job_id = c.job_id
WHERE c.rn = 1
  -- A spec is required to replay anything.
  AND COALESCE(j.original_spec, j.spec) IS NOT NULL
  AND LENGTH(TRIM(COALESCE(j.original_spec, j.spec))) > 0
ORDER BY c.classified_at DESC;
