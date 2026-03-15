"""MemoryStore — L3 knowledge graph CRUD, delegating to a MemoryStoreBackend."""

import logging

from .protocols import MemoryStoreBackend
from .types import MemoryNode

logger = logging.getLogger(__name__)


class MemoryStore:
    """High-level interface for the L3 persistent knowledge graph.

    All operations delegate to the underlying MemoryStoreBackend.
    """

    def __init__(self, backend: MemoryStoreBackend):
        self._backend = backend

    async def connect(self) -> None:
        await self._backend.connect()

    async def close(self) -> None:
        await self._backend.close()

    async def create_node(self, node: MemoryNode) -> str:
        return await self._backend.create_node(node)

    async def get_node(self, node_id: str) -> MemoryNode | None:
        return await self._backend.get_node(node_id)

    async def query_by_tags(self, project: str, tags: list[str], limit: int = 20) -> list[MemoryNode]:
        return await self._backend.query_by_tags(project, tags, limit)

    async def query_by_similarity(self, project: str, embedding: list[float], limit: int = 10) -> list[MemoryNode]:
        return await self._backend.query_by_similarity(project, embedding, limit)

    async def create_link(
        self,
        from_id: str,
        to_entity: str,
        link_type: str,
        confidence: float = 1.0,
        reasoning: str | None = None,
    ) -> None:
        await self._backend.create_link(from_id, to_entity, link_type, confidence, reasoning)

    async def get_backlinks(self, entity_name: str, project: str, limit: int = 20) -> list[MemoryNode]:
        return await self._backend.get_backlinks(entity_name, project, limit)

    async def ensure_entity(self, name: str, project: str, entity_type: str | None = None) -> str:
        return await self._backend.ensure_entity(name, project, entity_type)

    async def increment_access(self, node_id: str) -> None:
        await self._backend.increment_access(node_id)
