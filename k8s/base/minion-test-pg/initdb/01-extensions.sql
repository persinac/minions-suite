-- Run once by the postgres entrypoint, on a database that is recreated every
-- pod restart (the data volume is an emptyDir).
--
-- tests/conftest.py builds its schema from tests/conftest_pg_schema.sql, which
-- declares `public.vector(1536)`. Without this extension every DB-backed test
-- ERRORS at fixture setup rather than failing -- which reads as "my change broke
-- the suite" instead of "the fixture is missing", and has cost real debugging
-- time before. See CLAUDE.md, Tests.
CREATE EXTENSION IF NOT EXISTS vector;
