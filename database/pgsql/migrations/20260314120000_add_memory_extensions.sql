-- migrate:up

-- pgvector for embedding similarity search
CREATE EXTENSION IF NOT EXISTS vector;

-- NOTE: Apache AGE was removed here (2026-07-25).
--
-- The target database is DigitalOcean Managed Postgres, which does not offer the
-- `age` extension at all — it is absent from pg_available_extensions, so
-- `CREATE EXTENSION age` fails outright and cannot be enabled on a managed instance.
--
-- Dropping it costs nothing: no runtime code path uses AGE. The L3 backend
-- (agent_memory/backends/postgres.py) contains no Cypher and models the graph
-- relationally via memory_links -> memory_nodes. The single AGE reference lives in
-- the `agent-memory inspect --graph` diagnostic, which already degrades gracefully
-- and prints "AGE not available" by design. The `knowledge` graph this used to
-- create was never written to.

-- migrate:down

DROP EXTENSION IF EXISTS vector;
