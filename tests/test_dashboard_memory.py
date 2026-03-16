"""Tests for the dashboard memory page and API endpoints."""

import os

import psycopg
import pytest
from fastapi.testclient import TestClient

from minions.dashboard import app

TEST_PG_URL = os.getenv(
    "TEST_POSTGRES_URL",
    "postgresql://minion:minion@localhost:5434/minion",
)
TEST_SCHEMA = "minions_dashboard_test"


@pytest.fixture
def client():
    """Test client backed by a temporary Postgres schema."""
    import minions.dashboard as dash

    # Create test schema with minimal tables
    with psycopg.connect(TEST_PG_URL, autocommit=True) as conn:
        conn.execute(f"DROP SCHEMA IF EXISTS {TEST_SCHEMA} CASCADE")
        conn.execute(f"CREATE SCHEMA {TEST_SCHEMA}")
        conn.execute(f"SET search_path TO {TEST_SCHEMA}")
        conn.execute("""
            CREATE TABLE jobs (id TEXT, status TEXT, spec TEXT, created_at TIMESTAMPTZ DEFAULT NOW(), updated_at TIMESTAMPTZ DEFAULT NOW(), error TEXT, job_type TEXT, mr_url TEXT);
            CREATE TABLE tasks (id TEXT, job_id TEXT, status TEXT, title TEXT, service TEXT, agent_role TEXT, created_at TIMESTAMPTZ DEFAULT NOW(), pr_url TEXT, pr_number TEXT, error TEXT, verdict TEXT, comments_posted INTEGER DEFAULT 0, mr_url TEXT, revision_count INTEGER DEFAULT 0);
            CREATE TABLE agents (id TEXT, job_id TEXT, task_id TEXT, role TEXT, status TEXT, started_at TIMESTAMPTZ DEFAULT NOW(), finished_at TIMESTAMPTZ, log_file TEXT, error TEXT, input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0, cost_usd REAL DEFAULT 0, num_turns INTEGER DEFAULT 0, model TEXT);
            CREATE TABLE events (id SERIAL, job_id TEXT, event_type TEXT, detail TEXT, created_at TIMESTAMPTZ DEFAULT NOW());
            CREATE TABLE tool_calls (id SERIAL, job_id TEXT, tool_name TEXT, params JSONB, duration_ms REAL, error TEXT, created_at TIMESTAMPTZ DEFAULT NOW());
            CREATE TABLE messages (id TEXT, job_id TEXT, from_role TEXT, to_role TEXT, content TEXT, created_at TIMESTAMPTZ DEFAULT NOW());
            CREATE TABLE subtasks (id TEXT, task_id TEXT, description TEXT, status TEXT, sequence_num INTEGER, error TEXT);
            CREATE TABLE reviews (id TEXT);
            CREATE TABLE state_transitions (id SERIAL);
            CREATE TABLE heartbeats (agent_id TEXT);
            CREATE TABLE message_log (id SERIAL);
        """)

    # Point dashboard at test postgres
    dash._postgres_url = TEST_PG_URL

    # Patch _MINION_TABLES to use test schema
    original_tables = dash._PgConnectionWrapper._MINION_TABLES
    dash._PgConnectionWrapper._MINION_TABLES = tuple(
        list(original_tables) + [TEST_SCHEMA.replace("_", "")]
    )

    # Override schema qualification to use test schema
    original_execute = dash._PgConnectionWrapper.execute

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def patched_execute(self, sql, params=None):
        pg_sql = sql.replace("?", "%s")
        # Qualify with test schema instead of minions
        for tbl in original_tables:
            pg_sql = pg_sql.replace(f"FROM {tbl}", f"FROM {TEST_SCHEMA}.{tbl}")
            pg_sql = pg_sql.replace(f"JOIN {tbl}", f"JOIN {TEST_SCHEMA}.{tbl}")
        cursor = self._conn.cursor()
        await cursor.execute(pg_sql, params)
        yield dash._PgCursorWrapper(cursor)

    dash._PgConnectionWrapper.execute = patched_execute

    yield TestClient(app)

    dash._PgConnectionWrapper.execute = original_execute
    dash._PgConnectionWrapper._MINION_TABLES = original_tables

    with psycopg.connect(TEST_PG_URL, autocommit=True) as conn:
        conn.execute(f"DROP SCHEMA IF EXISTS {TEST_SCHEMA} CASCADE")


def test_index_returns_200(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Jobs" in resp.text
    assert "Memory" in resp.text


def test_memory_page_returns_200(client):
    resp = client.get("/memory")
    assert resp.status_code == 200
    assert "Memory" in resp.text
    assert "Knowledge Graph" in resp.text


def test_memory_page_graceful_without_memory_tables(client):
    """Memory page should not crash when memory_* tables don't exist."""
    resp = client.get("/memory")
    assert resp.status_code == 200
    assert "not available" in resp.text or "Memory" in resp.text


def test_memory_graph_api_returns_json(client):
    resp = client.get("/api/memory/graph")
    assert resp.status_code == 200
    data = resp.json()
    assert "nodes" in data
    assert "edges" in data
    assert isinstance(data["nodes"], list)
    assert isinstance(data["edges"], list)


def test_memory_operations_api_returns_json(client):
    resp = client.get("/api/memory/operations")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)


def test_metrics_endpoint_returns_200(client):
    resp = client.get("/metrics")
    assert resp.status_code == 200


def test_memory_summary_html_endpoint(client):
    resp = client.get("/api/memory/summary-html")
    assert resp.status_code == 200


def test_memory_operations_html_endpoint(client):
    resp = client.get("/api/memory/operations-html")
    assert resp.status_code == 200
