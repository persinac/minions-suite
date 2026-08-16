-- migrate:up
-- The spec analyst refines a raw ticket and submits the result, which overwrites
-- jobs.spec in place (see submit_refined_spec in minions/server/mcp.py). That is
-- what we want downstream -- every agent reads the refined text -- but it means
-- the human's original words are gone the moment refinement succeeds.
--
-- Keeping them matters for judging how a job went. Whether the analyst's stated
-- assumptions were reasonable is only answerable against the ticket that prompted
-- them; without the original there is nothing to compare the assumptions to.
--
-- NULL means "never refined" -- jobs.spec is still the original for those rows,
-- and for every row that predates this column.
ALTER TABLE minions.jobs ADD COLUMN IF NOT EXISTS original_spec TEXT;

-- migrate:down
ALTER TABLE minions.jobs DROP COLUMN IF EXISTS original_spec;
