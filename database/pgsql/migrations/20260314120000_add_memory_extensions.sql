-- migrate:up

-- pgvector for embedding similarity search
CREATE EXTENSION IF NOT EXISTS vector;

-- Apache AGE for graph queries (Cypher)
CREATE EXTENSION IF NOT EXISTS age;
LOAD 'age';
SET search_path = ag_catalog, "$user", public;
SELECT create_graph('knowledge');

-- migrate:down

SELECT drop_graph('knowledge', true);
DROP EXTENSION IF EXISTS age;
DROP EXTENSION IF EXISTS vector;
