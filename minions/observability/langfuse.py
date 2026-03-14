"""Langfuse OTEL logger factory for LiteLLM callback integration."""

import logging
from typing import Any

from ..config import Config

logger = logging.getLogger(__name__)


def create_langfuse_logger(config: Config) -> Any | None:
    """Create a LangfuseOtelLogger with a dedicated TracerProvider.

    Returns None when Langfuse is not configured (empty public key) or when
    initialization fails for any reason. LLM calls proceed unaffected.
    """
    if not config.langfuse_public_key:
        return None

    try:
        from litellm.integrations.langfuse.langfuse_otel import LangfuseOtelLogger
        from litellm.litellm_core_utils.safe_json_dumps import safe_dumps
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        class _TraceLevelLangfuseLogger(LangfuseOtelLogger):
            """Extends LangfuseOtelLogger to set trace-level input/output."""

            def set_attributes(self, span: Any, kwargs: Any, response_obj: Any) -> None:
                super().set_attributes(span, kwargs, response_obj)
                messages = kwargs.get("messages")
                if messages:
                    user_messages = [m for m in messages if m.get("role") == "user"]
                    if user_messages:
                        span.set_attribute("langfuse.trace.input", safe_dumps(user_messages[-1]))
                if response_obj and hasattr(response_obj, "get"):
                    choices = response_obj.get("choices", [])
                    if choices:
                        message = choices[0].get("message", {})
                        content = message.get("content")
                        if content:
                            span.set_attribute("langfuse.trace.output", content)

        otel_config = LangfuseOtelLogger.get_langfuse_otel_config()

        exporter = OTLPSpanExporter(
            endpoint=f"{config.langfuse_host}/v1/traces",
            headers=dict(item.split("=", 1) for item in otel_config.headers.split(",")) if otel_config.headers else {},
        )
        provider = TracerProvider(resource=Resource.create({"service.name": "minion-suite"}))
        provider.add_span_processor(BatchSpanProcessor(exporter))

        langfuse_logger = _TraceLevelLangfuseLogger(config=otel_config, callback_name="langfuse_otel")
        langfuse_logger.tracer = provider.get_tracer("litellm")

        logger.info("Langfuse OTEL logger created (endpoint: %s)", config.langfuse_host)
        return langfuse_logger

    except Exception:
        logger.warning("Failed to initialize Langfuse OTEL logger — continuing without tracing", exc_info=True)
        return None
