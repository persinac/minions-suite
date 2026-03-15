"""Shared fixtures for agent-memory tests."""

import uuid

import pytest

from agent_memory.types import MemoryNode


class InMemoryTupleSpaceBackend:
    """Simple in-memory TupleSpaceBackend for testing without Redis."""

    def __init__(self):
        self._store: dict[str, dict] = {}
        self._connected = False

    async def connect(self) -> None:
        self._connected = True

    async def close(self) -> None:
        self._connected = False

    async def put(self, key: str, doc: dict, ttl: int | None = None) -> None:
        self._store[key] = dict(doc)

    async def get(self, key: str) -> dict | None:
        return self._store.get(key)

    async def delete(self, key: str) -> bool:
        if key in self._store:
            del self._store[key]
            return True
        return False

    async def search(self, index: str, query: str, limit: int = 20) -> list[dict]:
        """Basic query parsing for @field:{value} patterns."""
        filters = {}
        for part in query.split():
            if part.startswith("@") and ":{" in part:
                field = part[1 : part.index(":")]
                value = part[part.index("{") + 1 : part.index("}")]
                filters[field] = value

        results = []
        for doc in self._store.values():
            match = True
            for field, value in filters.items():
                if field == "tags":
                    # Tags are stored comma-separated; OR semantics
                    doc_tags = set(doc.get("tags", "").split(","))
                    query_tags = set(value.split("|"))
                    if not doc_tags & query_tags:
                        match = False
                else:
                    if doc.get(field) != value:
                        match = False
            if match:
                results.append(doc)
        return results[:limit]

    async def atomic_pop(self, index: str, query: str) -> dict | None:
        results = await self.search(index, query, limit=1)
        if not results:
            return None
        doc = results[0]
        # Find and remove the key for this doc
        for key, stored in list(self._store.items()):
            if stored is doc or stored == doc:
                del self._store[key]
                return doc
        return doc

    async def keys(self, pattern: str) -> list[str]:
        prefix = pattern.replace("*", "")
        return [k for k in self._store if k.startswith(prefix)]

    async def create_index(self, name: str, schema: dict) -> None:
        pass  # No-op for in-memory backend


class InMemoryMemoryStoreBackend:
    """Simple in-memory MemoryStoreBackend for testing without Postgres."""

    def __init__(self):
        self._nodes: dict[str, MemoryNode] = {}
        self._links: list[dict] = []
        self._entities: dict[str, dict] = {}

    async def connect(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def create_node(self, node: MemoryNode) -> str:
        self._nodes[node.id] = node
        return node.id

    async def get_node(self, node_id: str) -> MemoryNode | None:
        return self._nodes.get(node_id)

    async def query_by_tags(self, project: str, tags: list[str], limit: int = 20) -> list[MemoryNode]:
        tag_set = set(tags)
        results = []
        for node in self._nodes.values():
            if node.project == project and tag_set & set(node.tags):
                results.append(node)
        return results[:limit]

    async def query_by_similarity(self, project: str, embedding: list[float], limit: int = 10) -> list[MemoryNode]:
        # Return all nodes for the project (mock doesn't do real similarity)
        results = [n for n in self._nodes.values() if n.project == project and n.embedding is not None]
        return results[:limit]

    async def create_link(self, from_id: str, to_entity: str, link_type: str, confidence: float = 1.0, reasoning: str | None = None) -> None:
        self._links.append(
            {
                "from_id": from_id,
                "to_entity": to_entity,
                "link_type": link_type,
                "confidence": confidence,
                "reasoning": reasoning,
            }
        )

    async def get_backlinks(self, entity_name: str, project: str, limit: int = 20) -> list[MemoryNode]:
        linked_ids = [lk["from_id"] for lk in self._links if lk["to_entity"] == entity_name]
        results = []
        for nid in linked_ids:
            node = self._nodes.get(nid)
            if node and node.project == project:
                results.append(node)
        return results[:limit]

    async def ensure_entity(self, name: str, project: str, entity_type: str | None = None) -> str:
        key = f"{name}:{project}"
        if key not in self._entities:
            self._entities[key] = {
                "id": uuid.uuid4().hex[:12],
                "name": name,
                "entity_type": entity_type,
                "project": project,
            }
        return self._entities[key]["id"]

    async def increment_access(self, node_id: str) -> None:
        node = self._nodes.get(node_id)
        if node:
            # Create a new node with incremented count (MemoryNode is immutable)
            updated = node.model_copy(update={"access_count": node.access_count + 1})
            self._nodes[node_id] = updated


@pytest.fixture
def ts_backend():
    """In-memory TupleSpaceBackend fixture."""
    return InMemoryTupleSpaceBackend()


@pytest.fixture
def store_backend():
    """In-memory MemoryStoreBackend fixture."""
    return InMemoryMemoryStoreBackend()
