## Why

Minion Suite runs multiple LLM-powered agents (code reviewer, spec analyst, arbiter, engineers, deploy monitor) but has no centralized observability into LLM calls — cost, latency, token usage, and prompt/completion content are only visible in local logs. Langfuse provides structured LLM tracing with session grouping, cost tracking, and prompt analytics. LiteLLM already supports Langfuse as a callback, so integration is lightweight. Reference implementation exists in svc-chatbot (MR !139).

## What Changes

- Add `langfuse>=4.0.0` dependency and OTEL exporter packages to `pyproject.toml`
- Add Langfuse configuration fields to `Config` dataclass (`LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_OTEL_HOST`)
- Create a Langfuse OTEL logger factory that builds a dedicated `TracerProvider` (avoids conflicts with any existing global telemetry)
- Wire the Langfuse callback into LiteLLM calls in `_agent_loop_generic()` via `litellm.callbacks`
- Inject trace metadata (job_id, task_id, agent_role) on each LiteLLM call for session/attribution grouping in Langfuse
- Add Langfuse env vars to `.env.example`, `docker-compose.yml`, and Dockerfile
- Add preflight check for Langfuse connectivity (optional/warn-only)

## Capabilities

### New Capabilities
- `langfuse-tracing`: LLM observability via Langfuse — callback setup, OTEL logger factory, trace metadata injection, and configuration

### Modified Capabilities
<!-- No existing specs to modify -->

## Impact

- **Dependencies**: `langfuse>=4.0.0` (brings `opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-http`, `opentelemetry-api`)
- **Config**: New optional env vars — zero-config when disabled (empty `LANGFUSE_PUBLIC_KEY` = no-op)
- **Agent runner**: `minions/agents/runner.py` — callback injection point in `_agent_loop_generic()`
- **Docker**: `docker-compose.yml`, `.env.example`, `Dockerfile` — env var passthrough
- **Preflight**: `minions/preflight.py` — optional connectivity check
- **No breaking changes**: Feature is opt-in via env vars, disabled by default
