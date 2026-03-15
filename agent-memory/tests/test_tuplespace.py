"""Tests for TupleSpace Linda semantics using in-memory backend."""

import pytest

from agent_memory.tuplespace import TupleSpace


@pytest.fixture
async def ts(ts_backend):
    space = TupleSpace(ts_backend, project="test-project")
    await space.connect()
    yield space
    await space.close()


async def test_out_returns_id(ts):
    fact_id = await ts.out(category="decision", key="db-choice", value="PostgreSQL")
    assert isinstance(fact_id, str)
    assert len(fact_id) > 0


async def test_rd_finds_published_fact(ts):
    await ts.out(category="decision", key="db-choice", value="PostgreSQL")
    facts = await ts.rd(category="decision")
    assert len(facts) == 1
    assert facts[0].value == "PostgreSQL"
    assert facts[0].category == "decision"
    assert facts[0].project == "test-project"


async def test_rd_filters_by_category(ts):
    await ts.out(category="decision", key="db", value="PG")
    await ts.out(category="observation", key="perf", value="slow")
    facts = await ts.rd(category="decision")
    assert len(facts) == 1
    assert facts[0].category == "decision"


async def test_rd_filters_by_tags(ts):
    await ts.out(category="decision", key="a", value="v1", tags=["auth"])
    await ts.out(category="decision", key="b", value="v2", tags=["infra"])
    facts = await ts.rd(category="decision", tags=["auth"])
    assert len(facts) == 1
    assert facts[0].value == "v1"


async def test_rd_returns_empty_for_wrong_project(ts_backend):
    ts1 = TupleSpace(ts_backend, project="project-a")
    ts2 = TupleSpace(ts_backend, project="project-b")
    await ts1.connect()
    await ts2.connect()
    await ts1.out(category="fact", key="k", value="v")
    facts = await ts2.rd(category="fact")
    assert len(facts) == 0


async def test_in_consumes_fact(ts):
    await ts.out(category="task", key="pending", value="review-PR-42")
    fact = await ts.in_(category="task", key_pattern="pending")
    assert fact is not None
    assert fact.value == "review-PR-42"
    # Fact should be gone
    remaining = await ts.rd(category="task")
    assert len(remaining) == 0


async def test_in_returns_none_when_empty(ts):
    fact = await ts.in_(category="task", key_pattern="nonexistent")
    assert fact is None


async def test_count(ts):
    await ts.out(category="decision", key="a", value="1")
    await ts.out(category="decision", key="b", value="2")
    await ts.out(category="observation", key="c", value="3")
    assert await ts.count("decision") == 2
    assert await ts.count("observation") == 1


async def test_expire_project(ts):
    await ts.out(category="a", key="1", value="x")
    await ts.out(category="b", key="2", value="y")
    removed = await ts.expire_project()
    assert removed == 2
    assert await ts.count() == 0


async def test_out_with_agent_metadata(ts):
    await ts.out(
        category="finding",
        key="sql-injection",
        value="found in handler.py",
        agent_role="CODE_REVIEWER",
        job_id="job-123",
    )
    facts = await ts.rd(category="finding")
    assert len(facts) == 1
    assert facts[0].agent_role == "CODE_REVIEWER"
    assert facts[0].job_id == "job-123"
