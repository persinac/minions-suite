"""Tests for L2→L3 archival."""

import pytest

from agent_memory.archiver import MemoryArchiver, _extract_entities
from agent_memory.store import MemoryStore
from agent_memory.tuplespace import TupleSpace


@pytest.fixture
async def ts(ts_backend):
    space = TupleSpace(ts_backend, project="test-project")
    await space.connect()
    yield space
    await space.close()


@pytest.fixture
async def store(store_backend):
    s = MemoryStore(store_backend)
    await s.connect()
    yield s
    await s.close()


async def test_archive_job_creates_nodes(ts, store):
    archiver = MemoryArchiver()
    await ts.out(category="decision", key="db", value="Use PostgreSQL", job_id="job-1", tags=["infra"])
    await ts.out(category="observation", key="perf", value="API latency 200ms", job_id="job-1", tags=["performance"])

    count = await archiver.archive_job(ts, store, "job-1", "test-project")
    assert count == 2

    # Verify nodes were created in L3
    nodes = await store.query_by_tags("test-project", ["infra"])
    assert len(nodes) >= 1


async def test_archive_job_no_facts(ts, store):
    archiver = MemoryArchiver()
    count = await archiver.archive_job(ts, store, "nonexistent-job", "test-project")
    assert count == 0


async def test_archive_job_temporal_edges(ts, store):
    archiver = MemoryArchiver()
    await ts.out(category="step", key="1", value="First thing", job_id="job-2", tags=["process"])
    await ts.out(category="step", key="2", value="Second thing", job_id="job-2", tags=["process"])
    await ts.out(category="step", key="3", value="Third thing", job_id="job-2", tags=["process"])

    count = await archiver.archive_job(ts, store, "job-2", "test-project")
    assert count == 3

    # Check that FOLLOWS links were created
    all_nodes = await store.query_by_tags("test-project", ["process"])
    assert len(all_nodes) == 3


async def test_archive_job_cleans_l2(ts, store):
    archiver = MemoryArchiver()
    await ts.out(category="fact", key="k1", value="v1", job_id="job-3")

    await archiver.archive_job(ts, store, "job-3", "test-project")

    # L2 should be cleaned up
    remaining = await ts.rd(category="fact")
    assert len(remaining) == 0


async def test_archive_job_extracts_entities(ts, store):
    archiver = MemoryArchiver()
    await ts.out(category="finding", key="auth", value="Updated src/auth/handler.py to use JWT", job_id="job-4", tags=["auth"])

    await archiver.archive_job(ts, store, "job-4", "test-project")

    # Should have linked to the file entity
    backlinks = await store.get_backlinks("src/auth/handler.py", "test-project")
    assert len(backlinks) >= 1


def test_extract_entities_file_paths():
    entities = _extract_entities("Modified src/auth/handler.py and utils/crypto.py")
    assert "src/auth/handler.py" in entities
    assert "utils/crypto.py" in entities


def test_extract_entities_module_names():
    entities = _extract_entities("Updated auth-module to use new crypto-utils")
    assert "auth-module" in entities
    assert "crypto-utils" in entities


def test_extract_entities_empty():
    assert _extract_entities("no entities here") == []
