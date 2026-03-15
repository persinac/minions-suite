"""Tests for MAGMA-style retrieval scoring and token budgeting."""

import time

from agent_memory.retrieval import _budget_tokens, _score_and_rank, get_file_backlinks, get_relevant_memories
from agent_memory.store import MemoryStore
from agent_memory.types import MemoryNode


def _make_node(
    nid: str, tags: list[str] | None = None, access_count: int = 0, links: list[str] | None = None, content: str = "x" * 100
) -> MemoryNode:
    return MemoryNode(
        id=nid,
        content=content,
        project="test",
        created_at=str(time.time()),
        tags=tags or [],
        access_count=access_count,
        links=links or [],
    )


def test_score_and_rank_prefers_high_access():
    low = _make_node("low", tags=["auth"], access_count=0)
    high = _make_node("high", tags=["auth"], access_count=50)
    ranked = _score_and_rank([low, high], {"auth"}, {"similarity": 0, "tag_overlap": 0, "recency": 0, "access_frequency": 1.0, "link_density": 0})
    assert ranked[0].id == "high"


def test_score_and_rank_prefers_tag_overlap():
    no_match = _make_node("no", tags=["infra"])
    match = _make_node("yes", tags=["auth"])
    ranked = _score_and_rank(
        [no_match, match], {"auth"}, {"similarity": 0, "tag_overlap": 1.0, "recency": 0, "access_frequency": 0, "link_density": 0}
    )
    assert ranked[0].id == "yes"


def test_score_and_rank_prefers_link_density():
    sparse = _make_node("sparse", links=[])
    dense = _make_node("dense", links=["a", "b", "c", "d", "e"])
    ranked = _score_and_rank([sparse, dense], set(), {"similarity": 0, "tag_overlap": 0, "recency": 0, "access_frequency": 0, "link_density": 1.0})
    assert ranked[0].id == "dense"


def test_budget_tokens_truncates():
    nodes = [_make_node(f"n{i}", content="x" * 400) for i in range(10)]
    # 400 chars = ~100 tokens per node, budget = 500 tokens = ~2000 chars => ~5 nodes
    result = _budget_tokens(nodes, max_tokens=500)
    assert len(result) < 10
    assert len(result) >= 1


def test_budget_tokens_returns_all_when_fits():
    nodes = [_make_node(f"n{i}", content="short") for i in range(3)]
    result = _budget_tokens(nodes, max_tokens=2000)
    assert len(result) == 3


async def test_get_relevant_memories_by_tags(store_backend):
    store = MemoryStore(store_backend)
    await store.create_node(_make_node("n1", tags=["auth"]))
    await store.create_node(_make_node("n2", tags=["infra"]))
    results = await get_relevant_memories(store, "test", tags=["auth"], max_tokens=2000)
    assert len(results) >= 1
    assert any(n.id == "n1" for n in results)


async def test_get_relevant_memories_empty_store(store_backend):
    store = MemoryStore(store_backend)
    results = await get_relevant_memories(store, "test", tags=["auth"])
    assert results == []


async def test_get_file_backlinks(store_backend):
    store = MemoryStore(store_backend)
    await store.create_node(_make_node("n1"))
    await store.create_link("n1", "src/auth/handler.py", "mentions")
    results = await get_file_backlinks(store, "test", ["src/auth/handler.py"])
    assert len(results) == 1
    assert results[0].id == "n1"


async def test_get_file_backlinks_empty(store_backend):
    store = MemoryStore(store_backend)
    results = await get_file_backlinks(store, "test", ["nonexistent.py"])
    assert results == []
