"""MAGMA-style retrieval + scoring for agent memory."""

import logging
import math
import time

from .store import MemoryStore
from .tracing import MemoryTraceEvent, TraceOp, emit
from .types import MemoryNode

logger = logging.getLogger(__name__)

# Default scoring weights
DEFAULT_WEIGHTS = {
    "similarity": 0.35,
    "tag_overlap": 0.20,
    "recency": 0.20,
    "access_frequency": 0.15,
    "link_density": 0.10,
}

# Rough chars-per-token estimate for budget enforcement
CHARS_PER_TOKEN = 4


async def get_relevant_memories(
    store: MemoryStore,
    project: str,
    query_embedding: list[float] | None = None,
    tags: list[str] | None = None,
    max_tokens: int = 2000,
    weights: dict[str, float] | None = None,
) -> list[MemoryNode]:
    """Retrieve and rank memories using MAGMA-style composite scoring."""
    t0 = time.monotonic()
    w = weights or DEFAULT_WEIGHTS
    candidates: dict[str, MemoryNode] = {}

    # Gather candidates from multiple signals
    if query_embedding:
        sim_results = await store.query_by_similarity(project, query_embedding, limit=30)
        for node in sim_results:
            candidates[node.id] = node

    if tags:
        tag_results = await store.query_by_tags(project, tags, limit=30)
        for node in tag_results:
            candidates[node.id] = node

    if not candidates:
        emit(
            MemoryTraceEvent(
                op=TraceOp.RETRIEVAL_RESULT,
                project=project,
                tier="retrieval",
                duration_ms=(time.monotonic() - t0) * 1000,
                details={"query_tags": tags or [], "has_embedding": query_embedding is not None, "result_count": 0, "candidates": 0},
            )
        )
        return []

    # Score each candidate
    query_tags = set(tags or [])
    scored = _score_and_rank(list(candidates.values()), query_tags, w, project)

    # Budget-constrain
    result = _budget_tokens(scored, max_tokens)

    emit(
        MemoryTraceEvent(
            op=TraceOp.RETRIEVAL_BUDGET,
            project=project,
            tier="retrieval",
            details={"candidates": len(candidates), "selected": len(result), "max_tokens": max_tokens, "dropped": len(candidates) - len(result)},
        )
    )
    emit(
        MemoryTraceEvent(
            op=TraceOp.RETRIEVAL_RESULT,
            project=project,
            tier="retrieval",
            duration_ms=(time.monotonic() - t0) * 1000,
            details={
                "query_tags": tags or [],
                "has_embedding": query_embedding is not None,
                "candidates": len(candidates),
                "result_count": len(result),
                "top_node_ids": [n.id for n in result[:5]],
            },
        )
    )
    return result


async def get_file_backlinks(
    store: MemoryStore,
    project: str,
    file_paths: list[str],
    max_tokens: int = 2000,
) -> list[MemoryNode]:
    """Retrieve memories linked to the given file paths via backlinks."""
    all_nodes: dict[str, MemoryNode] = {}
    for path in file_paths:
        backlinks = await store.get_backlinks(path, project, limit=10)
        for node in backlinks:
            all_nodes[node.id] = node

    if not all_nodes:
        return []

    # Simple recency ranking for file backlinks
    nodes = sorted(all_nodes.values(), key=lambda n: n.created_at, reverse=True)
    return _budget_tokens(nodes, max_tokens)


def _score_and_rank(
    nodes: list[MemoryNode],
    query_tags: set[str],
    weights: dict[str, float],
    project: str = "",
) -> list[MemoryNode]:
    """Score nodes with weighted composite and return sorted (highest first)."""
    now = time.time()

    scored: list[tuple[float, MemoryNode]] = []
    for node in nodes:
        # Tag overlap: Jaccard-style
        node_tags = set(node.tags)
        if query_tags and node_tags:
            tag_score = len(query_tags & node_tags) / len(query_tags | node_tags)
        else:
            tag_score = 0.0

        # Recency: exponential decay over 30 days
        try:
            age_seconds = now - float(node.created_at)
        except (ValueError, TypeError):
            age_seconds = 86400 * 30  # default to 30 days old
        recency_score = math.exp(-age_seconds / (86400 * 30))

        # Access frequency: log scale, capped
        access_score = min(1.0, math.log1p(node.access_count) / math.log1p(100))

        # Link density: more links = richer context
        link_score = min(1.0, len(node.links) / 5.0)

        # Similarity: assume nodes from similarity query are already ordered
        # Use position-based proxy (0th = best) since we don't have raw distances here
        sim_score = 0.5  # default middle score

        composite = (
            weights.get("similarity", 0.35) * sim_score
            + weights.get("tag_overlap", 0.20) * tag_score
            + weights.get("recency", 0.20) * recency_score
            + weights.get("access_frequency", 0.15) * access_score
            + weights.get("link_density", 0.10) * link_score
        )

        # Emit per-node scoring at DEBUG level
        emit(
            MemoryTraceEvent(
                op=TraceOp.RETRIEVAL_SCORE,
                project=project,
                tier="retrieval",
                details={
                    "node_id": node.id,
                    "composite": round(composite, 4),
                    "tag_score": round(tag_score, 4),
                    "recency_score": round(recency_score, 4),
                    "access_score": round(access_score, 4),
                    "link_score": round(link_score, 4),
                    "sim_score": round(sim_score, 4),
                    "access_count": node.access_count,
                    "tag_count": len(node.tags),
                    "link_count": len(node.links),
                },
            )
        )

        scored.append((composite, node))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [node for _, node in scored]


def _budget_tokens(nodes: list[MemoryNode], max_tokens: int) -> list[MemoryNode]:
    """Truncate the list to fit within the token budget."""
    max_chars = max_tokens * CHARS_PER_TOKEN
    result: list[MemoryNode] = []
    used = 0
    for node in nodes:
        node_chars = len(node.content) + len(node.title or "") + sum(len(t) for t in node.tags)
        if used + node_chars > max_chars:
            break
        result.append(node)
        used += node_chars
    return result
