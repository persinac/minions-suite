## ADDED Requirements

### Requirement: Docker Compose Redis service
The `docker-compose.yml` SHALL include a `redis` service using image `redis/redis-stack:latest` with healthcheck, port 6379 exposed, and a named volume `redisdata`.

#### Scenario: Redis service starts healthy
- **WHEN** running `docker compose up`
- **THEN** the redis service starts and passes its healthcheck

#### Scenario: App depends on Redis
- **WHEN** `memory_enabled` is True in the configuration
- **THEN** the `minion-suite` service's `depends_on` includes `redis`

### Requirement: Postgres image with AGE and pgvector
The Docker Compose postgres service SHALL use an image that provides both Apache AGE and pgvector extensions. Either `apache/age:PG17_latest` with pgvector layered on, or a custom Dockerfile combining both.

#### Scenario: Extensions available
- **WHEN** the postgres container starts
- **THEN** `CREATE EXTENSION IF NOT EXISTS vector` and `CREATE EXTENSION IF NOT EXISTS age` both succeed

### Requirement: Memory database migrations
The system SHALL include SQL migrations that create: `minions.memory_nodes` table (with vector column for embeddings), `minions.memory_links` table, `minions.memory_entities` table, plus GIN index on tags, ivfflat index on embedding, and btree indexes on project + created_at.

#### Scenario: Migration creates memory tables
- **WHEN** the memory migrations run against a fresh database
- **THEN** tables `memory_nodes`, `memory_links`, and `memory_entities` exist in the `minions` schema with all required columns and indexes

#### Scenario: AGE graph created
- **WHEN** the extensions migration runs
- **THEN** `ag_catalog.create_graph('knowledge')` creates the AGE graph for Cypher queries

### Requirement: Config fields for memory
The `Config` dataclass SHALL include fields: `memory_enabled: bool` (default False, env: `MEMORY_ENABLED`), `redis_url: str` (default "redis://localhost:6379", env: `REDIS_URL`), `redis_password: str` (default "", env: `REDIS_PASSWORD`), `memory_l3_token_budget: int` (default 2000, env: `MEMORY_L3_TOKEN_BUDGET`).

#### Scenario: Default config disables memory
- **WHEN** no `MEMORY_ENABLED` env var is set
- **THEN** `Config.from_env().memory_enabled` is False

#### Scenario: Config reads from environment
- **WHEN** `MEMORY_ENABLED=true` and `REDIS_URL=redis://myhost:6380` are set
- **THEN** `Config.from_env()` returns `memory_enabled=True` and `redis_url="redis://myhost:6380"`

### Requirement: Env example updated
The `.env.example` file SHALL include `MEMORY_ENABLED`, `REDIS_URL`, `REDIS_PASSWORD`, and `MEMORY_L3_TOKEN_BUDGET` with documented defaults.

#### Scenario: Env example has memory vars
- **WHEN** reading `.env.example`
- **THEN** it contains entries for `MEMORY_ENABLED=false`, `REDIS_URL=redis://localhost:6379`, `REDIS_PASSWORD=`, `MEMORY_L3_TOKEN_BUDGET=2000`

### Requirement: Redis preflight check
The `preflight.py` module SHALL include a `check_redis()` function that verifies Redis connectivity when `memory_enabled` is True. It SHALL be skipped when `memory_enabled` is False.

#### Scenario: Preflight passes with healthy Redis
- **WHEN** `memory_enabled` is True and Redis is reachable
- **THEN** `check_redis()` reports PASS

#### Scenario: Preflight fails with unreachable Redis
- **WHEN** `memory_enabled` is True and Redis is not reachable
- **THEN** `check_redis()` reports FAIL with connection error details

#### Scenario: Preflight skipped when disabled
- **WHEN** `memory_enabled` is False
- **THEN** `check_redis()` is not executed

### Requirement: Minions path dependency on agent-memory
The `minions/pyproject.toml` SHALL include `agent-memory` as a path dependency: `agent-memory = {path = "../agent-memory"}`.

#### Scenario: Dependency resolves
- **WHEN** running `uv sync` in the project root
- **THEN** the `agent_memory` package is installed from the local path

### Requirement: Zero regression when disabled
When `MEMORY_ENABLED` is False (default), all existing functionality SHALL work identically — no new imports at module level that require Redis/memory packages, no new prompt sections, no new tools in tool lists, no archival hooks.

#### Scenario: Existing tests pass unchanged
- **WHEN** `MEMORY_ENABLED` is not set (defaults to False)
- **WHEN** running `uv run pytest`
- **THEN** all 372+ existing tests pass without modification

#### Scenario: No memory tools in tool lists
- **WHEN** `memory_enabled` is False
- **WHEN** calling `get_tools_for_role("CODE_REVIEWER")`
- **THEN** the tool list is identical to the pre-change tool list
