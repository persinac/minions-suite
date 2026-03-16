"""OTEL span emission and persistence callbacks for agent-memory trace events."""

import collections
import json
import logging
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


def make_otel_callback(provider: Any) -> Callable:
    """Create a callback that emits OTEL spans for each MemoryTraceEvent.

    The provider should be an opentelemetry.sdk.trace.TracerProvider instance.
    Spans appear nested under the current Langfuse trace in the UI.
    """
    tracer = provider.get_tracer("agent-memory")

    def _callback(event) -> None:
        span = tracer.start_span(f"memory.{event.op}")
        try:
            span.set_attribute("memory.tier", event.tier or "")
            span.set_attribute("memory.op", event.op)
            span.set_attribute("memory.project", event.project or "")
            span.set_attribute("memory.job_id", event.job_id or "")
            span.set_attribute("memory.agent_role", event.agent_role or "")
            span.set_attribute("memory.duration_ms", event.duration_ms)

            # Flatten top-level detail keys as span attributes
            for k, v in event.details.items():
                attr_key = f"memory.detail.{k}"
                if isinstance(v, (str, int, float, bool)):
                    span.set_attribute(attr_key, v)
                elif isinstance(v, list):
                    span.set_attribute(attr_key, json.dumps(v, default=str))
                else:
                    span.set_attribute(attr_key, str(v))
        finally:
            span.end()

    return _callback


def make_persistence_callback(postgres_url: str) -> Callable:
    """Create a callback that buffers trace events and drains them to Postgres.

    Events are buffered in a deque and flushed when the buffer reaches 20 items
    or 5 seconds have elapsed since the last flush. Flushing happens inline
    (sync) using psycopg — acceptable because the insert is fast and batched.
    """
    _buffer: collections.deque = collections.deque(maxlen=500)
    _last_flush = time.time()
    _FLUSH_SIZE = 20
    _FLUSH_INTERVAL = 5.0

    def _flush() -> None:
        nonlocal _last_flush
        if not _buffer:
            return
        batch = []
        while _buffer:
            batch.append(_buffer.popleft())
        _last_flush = time.time()
        try:
            import psycopg

            with psycopg.connect(postgres_url, autocommit=True) as conn, conn.cursor() as cur:
                cur.executemany(
                    """INSERT INTO minions.memory_operations
                           (op, tier, project, job_id, agent_role, duration_ms, details, created_at)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, to_timestamp(%s))""",
                    [
                        (
                            e.op,
                            e.tier or "",
                            e.project or "",
                            e.job_id or "",
                            e.agent_role or "",
                            e.duration_ms,
                            json.dumps(e.details, default=str),
                            e.timestamp,
                        )
                        for e in batch
                    ],
                )
        except Exception:
            logger.debug("Failed to flush memory operations to Postgres", exc_info=True)

    def _callback(event) -> None:
        _buffer.append(event)
        elapsed = time.time() - _last_flush
        if len(_buffer) >= _FLUSH_SIZE or elapsed >= _FLUSH_INTERVAL:
            _flush()

    return _callback


def make_composite_callback(*callbacks: Callable) -> Callable:
    """Chain multiple trace callbacks into a single callback."""

    def _callback(event) -> None:
        for cb in callbacks:
            try:
                cb(event)
            except Exception:
                logger.debug("Trace callback failed", exc_info=True)

    return _callback
