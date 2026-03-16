"""TupleSpace — L2 shared cache using Linda coordination primitives."""

import logging
import time
import uuid

from .protocols import TupleSpaceBackend
from .tags import normalize_tags
from .tracing import MemoryTraceEvent, TraceOp, emit
from .types import Fact

logger = logging.getLogger(__name__)

INDEX_NAME = "facts"


def _fact_key(project: str, fact_id: str) -> str:
    return f"fact:{project}:{fact_id}"


class TupleSpace:
    """Linda-style tuplespace for real-time fact sharing between agents.

    All facts are scoped to the configured project.
    """

    def __init__(self, backend: TupleSpaceBackend, project: str):
        self._backend = backend
        self._project = project

    @property
    def project(self) -> str:
        return self._project

    async def connect(self) -> None:
        """Connect to the backend and ensure the index exists."""
        await self._backend.connect()
        await self._backend.create_index(
            INDEX_NAME,
            {
                "project": "TAG",
                "category": "TAG",
                "key": "TAG",
                "value": "TEXT",
                "tags": "TAG",
                "timestamp": "NUMERIC SORTABLE",
            },
        )

    async def close(self) -> None:
        await self._backend.close()

    async def out(
        self,
        category: str,
        key: str,
        value: str,
        tags: list[str] | None = None,
        agent_role: str | None = None,
        job_id: str | None = None,
        ttl: int | None = None,
    ) -> str:
        """Publish a fact to the tuplespace (Linda OUT)."""
        fact_id = uuid.uuid4().hex[:12]
        doc = {
            "project": self._project,
            "category": category,
            "key": key,
            "value": value,
            "tags": ",".join(normalize_tags(tags or [])),
            "agent_role": agent_role or "",
            "job_id": job_id or "",
            "timestamp": time.time(),
        }
        redis_key = _fact_key(self._project, fact_id)
        await self._backend.put(redis_key, doc, ttl=ttl)

        emit(
            MemoryTraceEvent(
                op=TraceOp.L2_PUT,
                project=self._project,
                job_id=job_id or "",
                agent_role=agent_role or "",
                tier="l2",
                details={"fact_id": fact_id, "category": category, "key": key, "ttl": ttl, "value_len": len(value)},
            )
        )
        return fact_id

    async def rd(
        self,
        category: str | None = None,
        key_pattern: str | None = None,
        tags: list[str] | None = None,
        limit: int = 20,
    ) -> list[Fact]:
        """Non-destructive query for matching facts (Linda RD)."""
        t0 = time.monotonic()
        query_parts = [f"@project:{{{self._project}}}"]
        if category:
            query_parts.append(f"@category:{{{category}}}")
        if key_pattern:
            query_parts.append(f"@key:{{{key_pattern}}}")
        if tags:
            normalized = normalize_tags(tags)
            tag_filter = "|".join(normalized)
            query_parts.append(f"@tags:{{{tag_filter}}}")

        query = " ".join(query_parts)
        results = await self._backend.search(INDEX_NAME, query, limit=limit)
        facts = [_doc_to_fact(doc) for doc in results]

        emit(
            MemoryTraceEvent(
                op=TraceOp.L2_READ,
                project=self._project,
                tier="l2",
                duration_ms=(time.monotonic() - t0) * 1000,
                details={"category": category, "key_pattern": key_pattern, "tags": tags, "result_count": len(facts)},
            )
        )
        return facts

    async def in_(
        self,
        category: str | None = None,
        key_pattern: str | None = None,
    ) -> Fact | None:
        """Atomically read and delete a matching fact (Linda IN)."""
        t0 = time.monotonic()
        query_parts = [f"@project:{{{self._project}}}"]
        if category:
            query_parts.append(f"@category:{{{category}}}")
        if key_pattern:
            query_parts.append(f"@key:{{{key_pattern}}}")

        query = " ".join(query_parts)
        doc = await self._backend.atomic_pop(INDEX_NAME, query)
        found = doc is not None

        emit(
            MemoryTraceEvent(
                op=TraceOp.L2_CONSUME,
                project=self._project,
                tier="l2",
                duration_ms=(time.monotonic() - t0) * 1000,
                details={"category": category, "key_pattern": key_pattern, "found": found},
            )
        )

        if doc is None:
            return None
        return _doc_to_fact(doc)

    async def count(self, category: str | None = None) -> int:
        """Count facts in the given category for this project."""
        query_parts = [f"@project:{{{self._project}}}"]
        if category:
            query_parts.append(f"@category:{{{category}}}")
        query = " ".join(query_parts)
        results = await self._backend.search(INDEX_NAME, query, limit=10000)
        return len(results)

    async def expire_project(self) -> int:
        """Remove all facts for this project. Returns count of removed facts."""
        pattern = _fact_key(self._project, "*")
        keys = await self._backend.keys(pattern)
        removed = 0
        for k in keys:
            if await self._backend.delete(k):
                removed += 1

        emit(
            MemoryTraceEvent(
                op=TraceOp.L2_EXPIRE,
                project=self._project,
                tier="l2",
                details={"removed": removed},
            )
        )
        return removed


def _doc_to_fact(doc: dict) -> Fact:
    """Convert a raw backend document to a Fact model."""
    tags_raw = doc.get("tags", "")
    if isinstance(tags_raw, str):
        tags = [t for t in tags_raw.split(",") if t]
    else:
        tags = list(tags_raw)

    return Fact(
        category=doc.get("category", ""),
        key=doc.get("key", ""),
        value=doc.get("value", ""),
        tags=tags,
        agent_role=doc.get("agent_role") or None,
        job_id=doc.get("job_id") or None,
        project=doc.get("project", ""),
        timestamp=float(doc.get("timestamp", 0)),
    )
