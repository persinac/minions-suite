r"""Shared fixtures for all tests.

Uses a real PostgreSQL database for test/prod parity, not SQLite. pgvector is
required, not just postgres — conftest_pg_schema.sql declares public.vector(1536),
so plain postgres:17 fails with `type "public.vector" does not exist`.

Requires postgres on localhost:5434 (user=minion, password=minion, db=minion);
override with TEST_POSTGRES_URL.

If you run `task up` you already have it — docker-compose.dev.yml's postgres is
the same image, port, and credentials, so no second container is needed. Otherwise
a standalone one is enough:

    docker run -d --name minion-test-pg \
      -e POSTGRES_USER=minion -e POSTGRES_PASSWORD=minion -e POSTGRES_DB=minion \
      -p 5434:5432 pgvector/pgvector:pg17
    docker exec minion-test-pg psql -U minion -d minion \
      -c "CREATE EXTENSION IF NOT EXISTS vector;"

Run one or the other, not both — they collide on :5434.

Without it, every DB-backed test errors at fixture setup rather than failing.
"""

import asyncio
import os
import sys
from pathlib import Path

import psycopg
import pytest

from minions.core.models import AgentRole, Task
from minions.db.postgres import PostgresDatabase

# Windows: psycopg async requires SelectorEventLoop, not ProactorEventLoop
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Test database connection — see the module docstring for how to start this postgres
TEST_PG_URL = os.getenv(
    "TEST_POSTGRES_URL",
    "postgresql://minion:minion@localhost:5434/minion",
)
TEST_SCHEMA = "minions_test"

_SCHEMA_SQL = (Path(__file__).parent / "conftest_pg_schema.sql").read_text(encoding="utf-8")

# All tables in dependency-safe truncation order (children before parents)
_TABLES = [
    "state_transitions",
    "heartbeats",
    "subtasks",
    "message_log",
    "events",
    "tool_calls",
    "messages",
    "agents",
    "tasks",
    "jobs",
]


@pytest.fixture(scope="session")
def pg_schema():
    """Create a test schema once per session, drop on teardown."""
    with psycopg.connect(TEST_PG_URL, autocommit=True) as conn:
        conn.execute(f"DROP SCHEMA IF EXISTS {TEST_SCHEMA} CASCADE")
        conn.execute(f"CREATE SCHEMA {TEST_SCHEMA}")
        conn.execute(f"SET search_path TO {TEST_SCHEMA}")
        conn.execute(_SCHEMA_SQL)
    yield TEST_SCHEMA
    with psycopg.connect(TEST_PG_URL, autocommit=True) as conn:
        conn.execute(f"DROP SCHEMA IF EXISTS {TEST_SCHEMA} CASCADE")


@pytest.fixture
async def db(pg_schema):
    """Per-test PostgresDatabase — truncates all tables for isolation."""
    import minions.db.postgres as pg_mod

    original_schema = pg_mod.JOB_SCHEMA
    pg_mod.JOB_SCHEMA = TEST_SCHEMA

    database = PostgresDatabase(TEST_PG_URL, pool_min=1, pool_max=3)
    await database.connect()

    yield database

    await database.close()

    # Truncate all tables for next test
    with psycopg.connect(TEST_PG_URL, autocommit=True) as conn:
        conn.execute(f"SET search_path TO {TEST_SCHEMA}")
        for table in _TABLES:
            conn.execute(f"TRUNCATE {TEST_SCHEMA}.{table} CASCADE")

    pg_mod.JOB_SCHEMA = original_schema


@pytest.fixture
async def sample_job(db):
    """A development job in spec_received state."""
    return await db.create_job("Implement user authentication")


@pytest.fixture
async def sample_review_job(db):
    """A review job with a CODE_REVIEWER task."""
    return await db.create_review_job(
        project="team/api",
        mr_url="https://gitlab.example.com/team/api/-/merge_requests/42",
        mr_id="42",
    )


@pytest.fixture
def make_task():
    """Factory for creating Task instances."""

    def _make(job_id, **overrides):
        defaults = {
            "job_id": job_id,
            "title": "Test task",
            "description": "A test task",
            "service": "api",
            "agent_role": AgentRole.BACKEND_ENGINEER,
        }
        defaults.update(overrides)
        return Task(**defaults)

    return _make
