## 1. Dependencies & Configuration

- [x] 1.1 Add `langfuse>=4.0.0` to `pyproject.toml` and run `uv lock`
- [x] 1.2 Add `langfuse_public_key`, `langfuse_secret_key`, and `langfuse_host` fields to `Config` dataclass with env var loading (`LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_OTEL_HOST`); default host to `https://cloud.langfuse.com`
- [x] 1.3 Add Langfuse env vars to `.env.example` with documentation comments
- [x] 1.4 Add Langfuse env var passthrough to `docker-compose.yml` for the minion-suite service

## 2. Langfuse Logger Factory

- [x] 2.1 Create `minions/observability/__init__.py` and `minions/observability/langfuse.py` module
- [x] 2.2 Implement `create_langfuse_logger(config: Config)` factory that returns a `LangfuseOtelLogger` instance or `None` when disabled/failed
- [x] 2.3 Inside the factory: create a dedicated `TracerProvider` with `BatchSpanProcessor` and `OTLPSpanExporter` pointed at `{config.langfuse_host}/v1/traces`, service name "minion-suite"
- [x] 2.4 Implement `_TraceLevelLangfuseLogger` subclass that overrides `set_attributes()` to add `langfuse.trace.input` (last user message) and `langfuse.trace.output` (first choice content)
- [x] 2.5 Wrap the entire factory in try/except — log warning and return `None` on any initialization failure

## 3. Callback Registration

- [x] 3.1 In `cli.py:_run_server()`, after config is loaded: call `create_langfuse_logger(config)` and if not `None`, append to `litellm.callbacks`; log "Langfuse tracing enabled"
- [x] 3.2 In `cli.py:_run_review()` (one-shot review path), do the same callback registration

## 4. Trace Metadata Injection

- [x] 4.1 In `_agent_loop_generic()`, add a `metadata` dict to the `litellm.acompletion()` call with keys: `trace_name` (agent role), `session_id` (job_id), `trace_user_id` (agent_id), `tags` (["minion-suite"])
- [x] 4.2 Handle the case where job_id or agent_id is not available (use empty strings)

## 5. Preflight Check

- [x] 5.1 Add an optional Langfuse preflight check in `preflight.py`: if `langfuse_public_key` is set, attempt an HTTP GET to `{langfuse_host}/api/public/health`; report PASS/WARN based on response
- [x] 5.2 If `langfuse_public_key` is empty, report WARN with "not configured — LLM tracing disabled"

## 6. Tests

- [x] 6.1 Unit test: `create_langfuse_logger()` returns `None` when `langfuse_public_key` is empty
- [x] 6.2 Unit test: `create_langfuse_logger()` returns a logger instance when keys are set (mock OTEL imports)
- [x] 6.3 Unit test: `create_langfuse_logger()` returns `None` and logs warning when OTEL import fails
- [x] 6.4 Unit test: verify `metadata` dict is passed in `acompletion()` calls within `_agent_loop_generic()` (mock litellm)
- [x] 6.5 Unit test: `Config.from_env()` loads Langfuse fields correctly
- [x] 6.6 Run full test suite (`uv run pytest`) to verify no regressions

## 7. Cleanup & Formatting

- [x] 7.1 Run `ruff format .` and `ruff check --fix .` on all new/modified files
- [x] 7.2 Verify Docker build succeeds with new dependency
