"""Tests for Prometheus memory metrics callback."""

import pytest
from agent_memory.tracing import MemoryTraceEvent

from minions.observability.memory_metrics import make_metrics_callback


@pytest.fixture(autouse=True)
def reset_prometheus_registry():
    """Reset the prometheus registry between tests to avoid duplicate collector errors."""
    import minions.observability.memory_metrics as mod

    mod._metrics_initialized = False
    mod._ops_total = None
    mod._op_duration = None
    mod._retrieval_candidates = None
    mod._retrieval_selected = None

    from prometheus_client import REGISTRY

    collectors = list(REGISTRY._names_to_collectors.values())
    for c in collectors:
        try:
            REGISTRY.unregister(c)
        except Exception:
            pass
    yield


def test_metrics_callback_not_none():
    callback = make_metrics_callback()
    assert callback is not None


def test_metrics_callback_increments_counter():
    from prometheus_client import REGISTRY

    callback = make_metrics_callback()

    event = MemoryTraceEvent(
        op="l3.create_node",
        tier="l3",
        project="test-project",
        duration_ms=15.0,
        details={},
    )
    callback(event)

    # Check counter was incremented
    sample = REGISTRY.get_sample_value(
        "memory_ops_total",
        {"tier": "l3", "op": "l3.create_node", "project": "test-project"},
    )
    assert sample == 1.0


def test_metrics_callback_records_duration():
    from prometheus_client import REGISTRY

    callback = make_metrics_callback()

    event = MemoryTraceEvent(
        op="l2.read",
        tier="l2",
        project="proj",
        duration_ms=250.0,
        details={},
    )
    callback(event)

    # Check histogram count
    count = REGISTRY.get_sample_value(
        "memory_op_duration_seconds_count",
        {"tier": "l2", "op": "l2.read"},
    )
    assert count == 1.0

    # Check histogram sum (250ms = 0.25s)
    total = REGISTRY.get_sample_value(
        "memory_op_duration_seconds_sum",
        {"tier": "l2", "op": "l2.read"},
    )
    assert abs(total - 0.25) < 0.001


def test_metrics_callback_tracks_retrieval_budget():
    from prometheus_client import REGISTRY

    callback = make_metrics_callback()

    event = MemoryTraceEvent(
        op="retrieval.budget",
        tier="retrieval",
        project="proj",
        details={"candidates": 25, "selected": 5, "max_tokens": 2000},
    )
    callback(event)

    cand_count = REGISTRY.get_sample_value("memory_retrieval_candidates_count")
    assert cand_count == 1.0

    sel_count = REGISTRY.get_sample_value("memory_retrieval_selected_count")
    assert sel_count == 1.0
