"""Tests for OTEL memory trace callback."""

import pytest
from agent_memory.tracing import MemoryTraceEvent
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

from minions.observability.memory_otel import make_composite_callback, make_otel_callback


class _InMemoryExporter(SpanExporter):
    """Minimal in-memory span exporter for tests."""

    def __init__(self):
        self._spans = []

    def export(self, spans):
        self._spans.extend(spans)
        return SpanExportResult.SUCCESS

    def get_finished_spans(self):
        return list(self._spans)

    def shutdown(self):
        pass

    def force_flush(self, timeout_millis=None):
        return True


@pytest.fixture
def otel_provider():
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    exporter = _InMemoryExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def test_otel_callback_emits_span(otel_provider):
    provider, exporter = otel_provider
    callback = make_otel_callback(provider)

    event = MemoryTraceEvent(
        op="l3.create_node",
        project="test-project",
        job_id="job-123",
        agent_role="code_reviewer",
        tier="l3",
        duration_ms=42.5,
        details={"node_id": "n1", "tags": ["auth", "pattern"], "has_embedding": True},
    )

    callback(event)

    spans = exporter.get_finished_spans()
    assert len(spans) == 1

    span = spans[0]
    assert span.name == "memory.l3.create_node"
    assert span.attributes["memory.tier"] == "l3"
    assert span.attributes["memory.op"] == "l3.create_node"
    assert span.attributes["memory.project"] == "test-project"
    assert span.attributes["memory.job_id"] == "job-123"
    assert span.attributes["memory.agent_role"] == "code_reviewer"
    assert span.attributes["memory.duration_ms"] == 42.5
    assert span.attributes["memory.detail.node_id"] == "n1"
    assert span.attributes["memory.detail.has_embedding"] is True


def test_otel_callback_handles_list_details(otel_provider):
    provider, exporter = otel_provider
    callback = make_otel_callback(provider)

    event = MemoryTraceEvent(
        op="retrieval.result",
        tier="retrieval",
        details={"query_tags": ["auth", "jwt"], "result_count": 5},
    )
    callback(event)

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    # Lists are serialized to JSON strings
    assert "auth" in spans[0].attributes["memory.detail.query_tags"]


def test_composite_callback_chains():
    called = []

    def cb1(event):
        called.append("cb1")

    def cb2(event):
        called.append("cb2")

    composite = make_composite_callback(cb1, cb2)
    event = MemoryTraceEvent(op="l2.put", tier="l2")
    composite(event)

    assert called == ["cb1", "cb2"]


def test_composite_callback_continues_on_error():
    called = []

    def cb_fail(event):
        raise RuntimeError("boom")

    def cb_ok(event):
        called.append("ok")

    composite = make_composite_callback(cb_fail, cb_ok)
    event = MemoryTraceEvent(op="l2.put", tier="l2")
    composite(event)

    assert called == ["ok"]
