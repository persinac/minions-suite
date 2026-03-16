"""Prometheus metrics callback for agent-memory trace events."""

import logging
from collections.abc import Callable

logger = logging.getLogger(__name__)

# Lazy-init metrics to avoid import-time side effects when prometheus_client
# is not installed or not desired.
_metrics_initialized = False
_ops_total = None
_op_duration = None
_retrieval_candidates = None
_retrieval_selected = None


def _ensure_metrics() -> None:
    global _metrics_initialized, _ops_total, _op_duration, _retrieval_candidates, _retrieval_selected
    if _metrics_initialized:
        return
    try:
        from prometheus_client import Counter, Histogram

        _ops_total = Counter(
            "memory_ops_total",
            "Total memory operations",
            ["tier", "op", "project"],
        )
        _op_duration = Histogram(
            "memory_op_duration_seconds",
            "Duration of memory operations",
            ["tier", "op"],
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
        )
        _retrieval_candidates = Histogram(
            "memory_retrieval_candidates",
            "Number of retrieval candidates scored",
            buckets=(1, 5, 10, 25, 50, 100, 250, 500),
        )
        _retrieval_selected = Histogram(
            "memory_retrieval_selected",
            "Number of retrieval results selected",
            buckets=(1, 2, 5, 10, 25, 50),
        )
        _metrics_initialized = True
    except ImportError:
        logger.debug("prometheus_client not installed — metrics callback disabled")


def make_metrics_callback() -> Callable | None:
    """Create a callback that increments Prometheus counters/histograms.

    Returns None if prometheus_client is not importable.
    """
    _ensure_metrics()
    if not _metrics_initialized:
        return None

    def _callback(event) -> None:
        tier = event.tier or "unknown"
        op = event.op
        project = event.project or ""

        _ops_total.labels(tier=tier, op=op, project=project).inc()

        if event.duration_ms > 0:
            _op_duration.labels(tier=tier, op=op).observe(event.duration_ms / 1000.0)

        # Track retrieval-specific histograms
        if op == "retrieval.budget":
            candidates = event.details.get("candidates", 0)
            selected = event.details.get("selected", 0)
            if candidates:
                _retrieval_candidates.observe(candidates)
            if selected:
                _retrieval_selected.observe(selected)

    return _callback
