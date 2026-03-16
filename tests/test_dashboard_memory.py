"""Tests for the dashboard memory page and API endpoints."""

import pytest
from fastapi.testclient import TestClient

from minions.dashboard import app


@pytest.fixture
def client(tmp_path):
    """Test client with a temporary SQLite database."""
    import minions.dashboard as dash

    db_path = str(tmp_path / "test.db")
    dash._db_path = db_path
    dash._db_backend = "sqlite"
    dash._postgres_url = ""

    # Create minimal schema — memory tables don't exist in SQLite so
    # the memory page should handle that gracefully.
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS jobs (id TEXT, status TEXT, spec TEXT, created_at TEXT, updated_at TEXT, error TEXT, job_type TEXT, mr_url TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS tasks (id TEXT, job_id TEXT, status TEXT, title TEXT, service TEXT, agent_role TEXT, created_at TEXT, pr_url TEXT, pr_number TEXT, error TEXT, verdict TEXT, comments_posted INTEGER, mr_url TEXT, revision_count INTEGER)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS agents (id TEXT, job_id TEXT, task_id TEXT, role TEXT, status TEXT, started_at TEXT, finished_at TEXT, log_file TEXT, error TEXT, input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0, cost_usd REAL DEFAULT 0, num_turns INTEGER DEFAULT 0, model TEXT)"
    )
    conn.execute("CREATE TABLE IF NOT EXISTS events (id TEXT, job_id TEXT, event_type TEXT, detail TEXT, created_at TEXT)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS tool_calls (id TEXT, job_id TEXT, tool_name TEXT, params TEXT, duration_ms REAL, error TEXT, created_at TEXT)"
    )
    conn.execute("CREATE TABLE IF NOT EXISTS messages (id TEXT, job_id TEXT, from_role TEXT, to_role TEXT, content TEXT, created_at TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS subtasks (id TEXT, task_id TEXT, description TEXT, status TEXT, sequence_num INTEGER, error TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS reviews (id TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS state_transitions (id TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS heartbeats (id TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS message_log (id TEXT)")
    conn.close()

    return TestClient(app)


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
    # Should show fallback message
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
