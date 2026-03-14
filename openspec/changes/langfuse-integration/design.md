## Context

Minion Suite agents make LLM calls via LiteLLM's `acompletion()` in `_agent_loop_generic()` (runner.py:333). Each agent turn produces usage/cost data logged locally, but there's no centralized observability. Langfuse provides structured LLM tracing — cost, latency, token usage, prompt/completion content — with session grouping and a web UI.

LiteLLM natively supports Langfuse as a callback via `litellm.callbacks`. The svc-chatbot integration (MR !139) proved the pattern: a dedicated `TracerProvider` exporting to Langfuse's OTEL endpoint, with a custom `LangfuseOtelLogger` subclass for trace-level input/output.

Minion Suite has no existing global `TracerProvider`, so the integration is simpler than svc-chatbot's — no conflict avoidance needed.

## Goals / Non-Goals

**Goals:**
- Opt-in Langfuse tracing for all LiteLLM calls across all agent roles
- Job/task/agent attribution in Langfuse traces (session grouping by job_id)
- Zero overhead when disabled (empty LANGFUSE_PUBLIC_KEY = no-op)
- Works in both local dev (SQLite) and production (Docker/K8s)

**Non-Goals:**
- Langfuse prompt management (using Langfuse to store/version prompts)
- Custom Langfuse dashboards or alerting
- Tracing non-LLM calls (DB, Git provider API, MCP)
- Migrating existing cost tracking to Langfuse (keep both)

## Decisions

### 1. Use LiteLLM's OTEL-based Langfuse callback (not the legacy SDK callback)

Langfuse v4 ships `LangfuseOtelLogger` which exports via OpenTelemetry. The older `LangfuseLogger` uses Langfuse's proprietary SDK directly.

**Choice:** OTEL-based (`LangfuseOtelLogger`)

**Why:** Aligns with svc-chatbot's proven pattern. OTEL is the industry standard for observability. Langfuse is deprecating the legacy callback path. The OTEL approach also means we get span-level detail (each tool call, each LLM turn) for free.

**Alternative considered:** Direct `langfuse` SDK integration (manual `trace()` / `generation()` calls). Rejected — more code, more maintenance, and LiteLLM already handles the instrumentation.

### 2. Create a dedicated module `minions/observability/langfuse.py`

**Choice:** New `observability/` package with a `langfuse.py` module containing the logger factory and configuration.

**Why:** Keeps observability concerns out of the agent runner. The factory is called once at startup and the callback is set globally on `litellm.callbacks`. If we add other observability backends later (e.g., Datadog LLM Observability), they live in the same package.

**Alternative considered:** Inline in `runner.py`. Rejected — runner.py is already 450+ lines and this is a cross-cutting concern.

### 3. Set `litellm.callbacks` globally at startup, not per-call

**Choice:** Configure the Langfuse callback once during server/engine startup (in `cli.py:_run_server()` and agent entry points), rather than passing `callbacks=` on each `acompletion()` call.

**Why:** LiteLLM's callback system is designed for global registration. Per-call callbacks would require threading the callback through `run_agent()` → `_agent_loop_generic()` → every `acompletion()` call. Global setup is simpler and matches the svc-chatbot pattern.

**Alternative considered:** Per-call injection via a wrapper around `acompletion()`. Rejected — over-engineered for a single callback.

### 4. Inject trace metadata via `litellm.acompletion(metadata=...)`

**Choice:** Pass a `metadata` dict on each `acompletion()` call with `trace_name`, `session_id` (job_id), `trace_user_id` (agent_role), and `tags`.

**Why:** Langfuse uses these fields for grouping and attribution. The `session_id` groups all traces for a job together. The metadata dict is already supported by LiteLLM and passed through to the callback. This requires a small change in `_agent_loop_generic()` to add the `metadata=` kwarg.

### 5. Subclass `LangfuseOtelLogger` for trace-level input/output

**Choice:** Create `_TraceLevelLangfuseLogger` that overrides `set_attributes()` to add `langfuse.trace.input` and `langfuse.trace.output` span attributes.

**Why:** Without this, Langfuse shows input/output at the span level but the trace-level view is empty. The svc-chatbot MR proved this subclass approach works. The override is minimal (~15 lines).

### 6. Configuration via existing `Config` dataclass

**Choice:** Add `langfuse_public_key`, `langfuse_secret_key`, and `langfuse_host` fields to the existing `Config` dataclass, read from env vars.

**Why:** Consistent with all other configuration in the project. No new config mechanism needed. Empty `langfuse_public_key` means disabled.

## Risks / Trade-offs

- **[Dependency size]** `langfuse>=4.0.0` pulls in OTEL SDK packages (~5 transitive deps). → Acceptable; OTEL packages are lightweight and well-maintained. Already present if using LangGraph's tracing.

- **[Global callback side effects]** Setting `litellm.callbacks` globally means ALL LiteLLM calls get traced, including any future non-agent usage. → Acceptable for now; if needed later, we can scope callbacks per-call.

- **[Langfuse availability]** If the Langfuse endpoint is unreachable, LiteLLM's callback execution is fire-and-forget (async batch export). → LLM calls are not blocked. OTEL's `BatchSpanProcessor` handles retries and drops silently on persistent failure.

- **[Token/cost in traces]** Langfuse will see full prompt/completion content including system prompts and tool outputs. → Expected for an observability tool. Ensure Langfuse instance is access-controlled.
