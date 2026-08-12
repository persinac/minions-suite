"""TupleSpace — L2 shared cache using Linda coordination primitives."""

import logging
import re
import time
import uuid

from .protocols import TupleSpaceBackend
from .tags import normalize_tags
from .tracing import MemoryTraceEvent, TraceOp, emit
from .types import Fact

# RediSearch treats these as syntax inside a TAG filter, so a raw value
# containing any of them produces a query error rather than a non-match:
#
#   @project:{wallet-api}  ->  Syntax error at offset 23 near api
#
# backends/redis.py swallows search exceptions and returns [], so the failure is
# indistinguishable from "nothing matched". Every hyphenated project, category,
# key or tag silently read back empty — which is most of them.
_TAG_SPECIAL = re.compile(r"([,.<>{}\[\]\"':;!@#$%^&*()\-+=~|/\\ ])")


def _escape_tag(value: str) -> str:
    """Escape a value for use inside a RediSearch TAG filter: {value}."""
    return _TAG_SPECIAL.sub(r"\\\1", str(value))


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
        project: str | None = None,
    ) -> str:
        """Publish a fact to the tuplespace (Linda OUT).

        `project` overrides the instance scope for this write. The server builds
        one TupleSpace at startup scoped to the *first* entry in projects.yaml
        and every job shares it, so without an override every fact -- whichever
        repo the job targets -- files under that one project. Reads for the
        project that actually did the work then come back empty, and because the
        Redis backend swallows search errors and returns [], that is
        indistinguishable from "nothing learned yet".
        """
        write_project = project or self._project
        fact_id = uuid.uuid4().hex[:12]
        doc = {
            "project": write_project,
            "category": category,
            "key": key,
            "value": value,
            "tags": ",".join(normalize_tags(tags or [])),
            "agent_role": agent_role or "",
            "job_id": job_id or "",
            "timestamp": time.time(),
        }
        redis_key = _fact_key(write_project, fact_id)
        await self._backend.put(redis_key, doc, ttl=ttl)

        emit(
            MemoryTraceEvent(
                op=TraceOp.L2_PUT,
                project=write_project,
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
        project: str | None = None,
    ) -> list[Fact]:
        """Non-destructive query for matching facts (Linda RD).

        `project` overrides the instance scope for this read, mirroring `out`.
        Without it a read is pinned to the startup scope while writes may have
        been steered elsewhere, so the two halves address different namespaces
        and every query comes back empty.
        """
        t0 = time.monotonic()
        read_project = project or self._project
        query_parts = [f"@project:{{{_escape_tag(read_project)}}}"]
        if category:
            query_parts.append(f"@category:{{{_escape_tag(category)}}}")
        if key_pattern:
            query_parts.append(f"@key:{{{_escape_tag(key_pattern)}}}")
        if tags:
            normalized = normalize_tags(tags)
            tag_filter = "|".join(_escape_tag(x) for x in normalized)
            query_parts.append(f"@tags:{{{tag_filter}}}")

        query = " ".join(query_parts)
        results = await self._backend.search(INDEX_NAME, query, limit=limit)
        facts = [_doc_to_fact(doc) for doc in results]

        emit(
            MemoryTraceEvent(
                op=TraceOp.L2_READ,
                project=read_project,
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
        query_parts = [f"@project:{{{_escape_tag(self._project)}}}"]
        if category:
            query_parts.append(f"@category:{{{_escape_tag(category)}}}")
        if key_pattern:
            query_parts.append(f"@key:{{{_escape_tag(key_pattern)}}}")

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
        query_parts = [f"@project:{{{_escape_tag(self._project)}}}"]
        if category:
            query_parts.append(f"@category:{{{_escape_tag(category)}}}")
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
