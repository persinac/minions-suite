"""Tests for MemoryStore using in-memory backend."""

import time

import pytest

from agent_memory.store import MemoryStore
from agent_memory.types import MemoryNode


def _make_node(nid: str = "n1", project: str = "test", **kwargs) -> MemoryNode:
    defaults = {
        "id": nid,
        "content": f"Content for {nid}",
        "project": project,
        "created_at": str(time.time()),
        "tags": [],
    }
    defaults.update(kwargs)
    return MemoryNode(**defaults)


@pytest.fixture
async def store(store_backend):
    s = MemoryStore(store_backend)
    await s.connect()
    yield s
    await s.close()


async def test_create_and_get_node(store):
    node = _make_node("n1")
    await store.create_node(node)
    retrieved = await store.get_node("n1")
    assert retrieved is not None
    assert retrieved.id == "n1"
    assert retrieved.content == "Content for n1"


async def test_get_nonexistent_node(store):
    result = await store.get_node("nonexistent")
    assert result is None


async def test_query_by_tags_single(store):
    await store.create_node(_make_node("n1", tags=["auth", "security"]))
    await store.create_node(_make_node("n2", tags=["infra"]))
    results = await store.query_by_tags("test", ["auth"])
    assert len(results) == 1
    assert results[0].id == "n1"


async def test_query_by_tags_or_semantics(store):
    await store.create_node(_make_node("n1", tags=["auth"]))
    await store.create_node(_make_node("n2", tags=["infra"]))
    results = await store.query_by_tags("test", ["auth", "infra"])
    assert len(results) == 2


async def test_query_by_tags_scoped_to_project(store):
    await store.create_node(_make_node("n1", project="proj-a", tags=["auth"]))
    await store.create_node(_make_node("n2", project="proj-b", tags=["auth"]))
    results = await store.query_by_tags("proj-a", ["auth"])
    assert len(results) == 1
    assert results[0].project == "proj-a"


async def test_query_by_similarity(store):
    await store.create_node(_make_node("n1", embedding=[0.1, 0.2, 0.3]))
    await store.create_node(_make_node("n2", embedding=[0.4, 0.5, 0.6]))
    await store.create_node(_make_node("n3"))  # no embedding
    results = await store.query_by_similarity("test", [0.1, 0.2, 0.3], limit=2)
    assert len(results) == 2
    # All returned nodes should have embeddings
    for r in results:
        assert r.embedding is not None


async def test_ensure_entity_idempotent(store):
    id1 = await store.ensure_entity("auth-module", "test", "module")
    id2 = await store.ensure_entity("auth-module", "test", "module")
    assert id1 == id2


async def test_create_link_and_backlinks(store):
    await store.create_node(_make_node("n1"))
    await store.ensure_entity("auth-module", "test", "module")
    await store.create_link("n1", "auth-module", "mentions")
    backlinks = await store.get_backlinks("auth-module", "test")
    assert len(backlinks) == 1
    assert backlinks[0].id == "n1"


async def test_backlinks_scoped_to_project(store):
    await store.create_node(_make_node("n1", project="proj-a"))
    await store.create_node(_make_node("n2", project="proj-b"))
    await store.create_link("n1", "auth-module", "mentions")
    await store.create_link("n2", "auth-module", "mentions")
    backlinks = await store.get_backlinks("auth-module", "proj-a")
    assert len(backlinks) == 1
    assert backlinks[0].project == "proj-a"


async def test_increment_access(store):
    await store.create_node(_make_node("n1"))
    await store.increment_access("n1")
    await store.increment_access("n1")
    node = await store.get_node("n1")
    assert node.access_count == 2
