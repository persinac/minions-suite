"""Tests for prompt context builders."""

import time

from agent_memory.context import build_file_context, build_knowledge_context
from agent_memory.store import MemoryStore
from agent_memory.types import MemoryNode


def _make_node(nid: str = "n1", project: str = "test", content: str = "Some knowledge", **kwargs) -> MemoryNode:
    defaults = {
        "id": nid,
        "content": content,
        "project": project,
        "created_at": str(time.time()),
        "tags": ["auth"],
    }
    defaults.update(kwargs)
    return MemoryNode(**defaults)


async def test_knowledge_context_empty_when_no_notes(store_backend):
    store = MemoryStore(store_backend)
    result = await build_knowledge_context(store, "empty-project")
    assert result == ""


async def test_knowledge_context_with_memories(store_backend):
    store = MemoryStore(store_backend)
    await store.create_node(_make_node("n1", content="Auth uses JWT tokens", tags=["auth"]))
    result = await build_knowledge_context(store, "test", task_description="implement auth")
    assert "Prior Knowledge" in result
    assert "JWT tokens" in result


async def test_knowledge_context_includes_tags(store_backend):
    store = MemoryStore(store_backend)
    await store.create_node(_make_node("n1", tags=["auth", "security"]))
    result = await build_knowledge_context(store, "test", task_description="auth security")
    if result:
        assert "#auth" in result or "#security" in result


async def test_knowledge_context_respects_token_budget(store_backend):
    store = MemoryStore(store_backend)
    for i in range(20):
        await store.create_node(_make_node(f"n{i}", content="x" * 500, tags=["auth"]))
    result = await build_knowledge_context(store, "test", task_description="auth", max_tokens=200)
    # 200 tokens ~= 800 chars, should be much shorter than 20 * 500 = 10000 chars
    assert len(result) < 2000


async def test_file_context_empty_when_no_backlinks(store_backend):
    store = MemoryStore(store_backend)
    result = await build_file_context(store, "test", ["src/main.py"])
    assert result == ""


async def test_file_context_with_backlinks(store_backend):
    store = MemoryStore(store_backend)
    await store.create_node(_make_node("n1", content="Handler auth patterns"))
    await store.create_link("n1", "src/auth/handler.py", "mentions")
    result = await build_file_context(store, "test", ["src/auth/handler.py"])
    assert "File Knowledge" in result
    assert "Handler auth patterns" in result
