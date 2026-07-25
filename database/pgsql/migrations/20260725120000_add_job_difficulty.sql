-- migrate:up
-- Difficulty drives model tier selection for the whole job (see minions/classifier.py).
-- NULL means "not classified" and falls back to the default model, so existing
-- rows and a disabled classifier both behave exactly as before.
ALTER TABLE minions.jobs ADD COLUMN IF NOT EXISTS difficulty TEXT;

-- migrate:down
ALTER TABLE minions.jobs DROP COLUMN IF EXISTS difficulty;
