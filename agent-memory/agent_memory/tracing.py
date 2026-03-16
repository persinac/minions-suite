"""Structured tracing for the memory system.

Every memory operation emits a MemoryTraceEvent so you can reconstruct
exactly what happened: which tier was queried, what scored how, what got
injected into the prompt, what was archived, what was evicted.

Events are emitted via a pluggable callback (default: logging). Set a
custom callback via set_trace_callback() to pipe into Langfuse, OTEL, etc.
"""

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from enum import StrEnum

logger = logging.getLogger("agent_memory.trace")


class TraceOp(StrEnum):
    # L2 operations
    L2_PUT = "l2.put"
    L2_READ = "l2.read"
    L2_CONSUME = "l2.consume"
    L2_EXPIRE = "l2.expire"

    # L3 operations
    L3_CREATE_NODE = "l3.create_node"
    L3_QUERY_TAGS = "l3.query_tags"
    L3_QUERY_SIMILARITY = "l3.query_similarity"
    L3_BACKLINKS = "l3.backlinks"
    L3_ENTITY_ENSURE = "l3.entity_ensure"
    L3_ACCESS_INCREMENT = "l3.access_increment"

    # Retrieval
    RETRIEVAL_SCORE = "retrieval.score"
    RETRIEVAL_BUDGET = "retrieval.budget"
    RETRIEVAL_RESULT = "retrieval.result"

    # Context building
    CONTEXT_KNOWLEDGE = "context.knowledge"
    CONTEXT_FILE = "context.file"

    # Archival
    ARCHIVE_START = "archive.start"
    ARCHIVE_NODE = "archive.node"
    ARCHIVE_EDGE = "archive.edge"
    ARCHIVE_ENTITY = "archive.entity"
    ARCHIVE_CLEANUP = "archive.cleanup"
    ARCHIVE_COMPLETE = "archive.complete"

    # Decay
    DECAY_SCAN = "decay.scan"
    DECAY_NODE = "decay.node"


@dataclass
class MemoryTraceEvent:
    op: str
    project: str = ""
    job_id: str = ""
    agent_role: str = ""
    tier: str = ""  # "l2", "l3", "retrieval", "context", "archive"
    duration_ms: float = 0.0
    details: dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), default=str)


# Pluggable callback — default logs as structured JSON
_trace_callback = None


def set_trace_callback(callback) -> None:
    """Set a custom trace callback: fn(MemoryTraceEvent) -> None.

    Use this to pipe traces into Langfuse, OTEL, or a custom collector.
    """
    global _trace_callback
    _trace_callback = callback


def emit(event: MemoryTraceEvent) -> None:
    """Emit a trace event via the configured callback."""
    if _trace_callback:
        try:
            _trace_callback(event)
        except Exception:
            logger.debug("Trace callback failed", exc_info=True)

    # Always log at DEBUG level for file-based tracing
    logger.debug("MEMORY_TRACE %s", event.to_json())

    # At INFO level, emit a human-readable summary for key operations
    if logger.isEnabledFor(logging.INFO):
        summary = _summarize(event)
        if summary:
            logger.info(summary)


def _summarize(event: MemoryTraceEvent) -> str | None:
    """Human-readable one-liner for key operations."""
    d = event.details
    op = event.op

    if op == TraceOp.L2_PUT:
        return f"[L2] PUT {d.get('category', '?')}:{d.get('key', '?')} project={event.project} ttl={d.get('ttl', 'none')}"
    if op == TraceOp.L2_READ:
        return f"[L2] READ {d.get('category', '*')} -> {d.get('result_count', 0)} facts ({event.duration_ms:.0f}ms)"
    if op == TraceOp.L2_CONSUME:
        found = "found" if d.get("found") else "empty"
        return f"[L2] CONSUME {d.get('category', '*')} -> {found} ({event.duration_ms:.0f}ms)"
    if op == TraceOp.L2_EXPIRE:
        return f"[L2] EXPIRE project={event.project} -> {d.get('removed', 0)} facts"

    if op == TraceOp.L3_CREATE_NODE:
        return f"[L3] NODE {d.get('node_id', '?')} tags={d.get('tags', [])} has_embedding={d.get('has_embedding', False)}"
    if op == TraceOp.L3_QUERY_TAGS:
        return f"[L3] QUERY tags={d.get('tags', [])} -> {d.get('result_count', 0)} nodes ({event.duration_ms:.0f}ms)"
    if op == TraceOp.L3_QUERY_SIMILARITY:
        return f"[L3] SIMILARITY project={event.project} -> {d.get('result_count', 0)} nodes ({event.duration_ms:.0f}ms)"
    if op == TraceOp.L3_BACKLINKS:
        return f"[L3] BACKLINKS entity={d.get('entity', '?')} -> {d.get('result_count', 0)} nodes ({event.duration_ms:.0f}ms)"

    if op == TraceOp.RETRIEVAL_SCORE:
        return None  # Too noisy at INFO — available at DEBUG via JSON
    if op == TraceOp.RETRIEVAL_BUDGET:
        return f"[RETRIEVAL] BUDGET {d.get('candidates', 0)} candidates -> {d.get('selected', 0)} within {d.get('max_tokens', 0)} tokens"
    if op == TraceOp.RETRIEVAL_RESULT:
        return (
            f"[RETRIEVAL] project={event.project} tags={d.get('query_tags', [])} -> {d.get('result_count', 0)} memories ({event.duration_ms:.0f}ms)"
        )

    if op == TraceOp.CONTEXT_KNOWLEDGE:
        return f"[CONTEXT] KNOWLEDGE project={event.project} -> {d.get('chars', 0)} chars, {d.get('node_count', 0)} nodes"
    if op == TraceOp.CONTEXT_FILE:
        return f"[CONTEXT] FILE project={event.project} files={d.get('file_count', 0)} -> {d.get('chars', 0)} chars, {d.get('node_count', 0)} nodes"

    if op == TraceOp.ARCHIVE_START:
        return f"[ARCHIVE] START job={event.job_id} project={event.project} facts={d.get('fact_count', 0)}"
    if op == TraceOp.ARCHIVE_COMPLETE:
        return f"[ARCHIVE] DONE job={event.job_id} -> {d.get('nodes', 0)} nodes, {d.get('edges', 0)} edges, {d.get('entities', 0)} entities ({event.duration_ms:.0f}ms)"
    if op == TraceOp.ARCHIVE_CLEANUP:
        return f"[ARCHIVE] CLEANUP job={event.job_id} -> {d.get('removed', 0)} L2 facts removed"

    if op == TraceOp.DECAY_SCAN:
        return f"[DECAY] SCAN project={event.project} -> {d.get('eligible', 0)}/{d.get('total', 0)} nodes eligible for decay"

    return None
