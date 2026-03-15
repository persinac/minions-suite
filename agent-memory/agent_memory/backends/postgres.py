"""Postgres backend for the L3 knowledge graph — psycopg3 + pgvector + AGE."""

import json
import logging
import uuid

from agent_memory.types import MemoryNode

logger = logging.getLogger(__name__)


class PostgresMemoryBackend:
    """MemoryStoreBackend implementation using psycopg3 async pool with pgvector and AGE."""

    def __init__(self, pool=None, conninfo: str | None = None):
        self._pool = pool
        self._conninfo = conninfo
        self._owns_pool = pool is None

    async def connect(self) -> None:
        if self._pool is None:
            import psycopg_pool

            self._pool = psycopg_pool.AsyncConnectionPool(self._conninfo, min_size=1, max_size=5)
            await self._pool.open()

    async def close(self) -> None:
        if self._owns_pool and self._pool:
            await self._pool.close()

    async def create_node(self, node: MemoryNode) -> str:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            embedding_val = node.embedding if node.embedding else None
            await cur.execute(
                """
                INSERT INTO minions.memory_nodes
                    (id, content, title, tags, embedding, attributes,
                     source_job_id, source_agent_role, project, access_count)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (
                    node.id,
                    node.content,
                    node.title,
                    node.tags,
                    embedding_val,
                    json.dumps(node.attributes),
                    node.source_job_id,
                    node.source_agent_role,
                    node.project,
                    node.access_count,
                ),
            )
        return node.id

    async def get_node(self, node_id: str) -> MemoryNode | None:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, content, title, tags, embedding, attributes,
                       source_job_id, source_agent_role, project, access_count, created_at
                FROM minions.memory_nodes WHERE id = %s
                """,
                (node_id,),
            )
            row = await cur.fetchone()
            if row is None:
                return None
            return _row_to_node(row)

    async def query_by_tags(self, project: str, tags: list[str], limit: int = 20) -> list[MemoryNode]:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, content, title, tags, embedding, attributes,
                       source_job_id, source_agent_role, project, access_count, created_at
                FROM minions.memory_nodes
                WHERE project = %s AND tags && %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (project, tags, limit),
            )
            rows = await cur.fetchall()
            return [_row_to_node(r) for r in rows]

    async def query_by_similarity(self, project: str, embedding: list[float], limit: int = 10) -> list[MemoryNode]:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, content, title, tags, embedding, attributes,
                       source_job_id, source_agent_role, project, access_count, created_at
                FROM minions.memory_nodes
                WHERE project = %s AND embedding IS NOT NULL
                ORDER BY embedding <=> %s::vector
                LIMIT %s
                """,
                (project, str(embedding), limit),
            )
            rows = await cur.fetchall()
            return [_row_to_node(r) for r in rows]

    async def create_link(
        self,
        from_id: str,
        to_entity: str,
        link_type: str,
        confidence: float = 1.0,
        reasoning: str | None = None,
    ) -> None:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO minions.memory_links (from_node, to_entity, link_type, confidence, reasoning)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (from_id, to_entity, link_type, confidence, reasoning),
            )

    async def get_backlinks(self, entity_name: str, project: str, limit: int = 20) -> list[MemoryNode]:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                SELECT n.id, n.content, n.title, n.tags, n.embedding, n.attributes,
                       n.source_job_id, n.source_agent_role, n.project, n.access_count, n.created_at
                FROM minions.memory_nodes n
                JOIN minions.memory_links l ON l.from_node = n.id
                WHERE l.to_entity = %s AND n.project = %s
                ORDER BY n.created_at DESC
                LIMIT %s
                """,
                (entity_name, project, limit),
            )
            rows = await cur.fetchall()
            return [_row_to_node(r) for r in rows]

    async def ensure_entity(self, name: str, project: str, entity_type: str | None = None) -> str:
        entity_id = uuid.uuid4().hex[:12]
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO minions.memory_entities (id, name, entity_type, project)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (name, project) DO NOTHING
                RETURNING id
                """,
                (entity_id, name, entity_type, project),
            )
            row = await cur.fetchone()
            if row:
                return row[0]
            # Already existed — fetch existing ID
            await cur.execute(
                "SELECT id FROM minions.memory_entities WHERE name = %s AND project = %s",
                (name, project),
            )
            row = await cur.fetchone()
            return row[0]

    async def increment_access(self, node_id: str) -> None:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE minions.memory_nodes
                SET access_count = access_count + 1, last_accessed = now()
                WHERE id = %s
                """,
                (node_id,),
            )


def _row_to_node(row: tuple) -> MemoryNode:
    """Convert a database row to a MemoryNode."""
    attrs = row[5]
    if isinstance(attrs, str):
        attrs = json.loads(attrs)

    return MemoryNode(
        id=row[0],
        content=row[1],
        title=row[2],
        tags=list(row[3]) if row[3] else [],
        embedding=list(row[4]) if row[4] else None,
        attributes=attrs or {},
        source_job_id=row[6],
        source_agent_role=row[7],
        project=row[8],
        access_count=row[9] or 0,
        created_at=str(row[10]) if row[10] else "",
    )
