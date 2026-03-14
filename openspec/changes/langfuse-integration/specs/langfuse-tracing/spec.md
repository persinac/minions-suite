## ADDED Requirements

### Requirement: Langfuse callback configuration
The system SHALL configure a Langfuse OTEL callback on `litellm.callbacks` when `LANGFUSE_PUBLIC_KEY` is set and non-empty. The system SHALL NOT configure any Langfuse callback when `LANGFUSE_PUBLIC_KEY` is empty or unset.

#### Scenario: Langfuse enabled with valid keys
- **WHEN** `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` environment variables are set to non-empty values
- **THEN** the system registers a `LangfuseOtelLogger` instance on `litellm.callbacks` at startup

#### Scenario: Langfuse disabled by default
- **WHEN** `LANGFUSE_PUBLIC_KEY` is empty or unset
- **THEN** the system does not register any Langfuse callback and does not import Langfuse packages

### Requirement: Dedicated TracerProvider
The system SHALL create a dedicated OpenTelemetry `TracerProvider` for Langfuse export, separate from any global `TracerProvider`. The provider SHALL use a `BatchSpanProcessor` with an `OTLPSpanExporter` pointed at the configured Langfuse OTEL endpoint.

#### Scenario: TracerProvider isolation
- **WHEN** the Langfuse callback is configured
- **THEN** the system creates a new `TracerProvider` instance (not the global provider) with service name "minion-suite"

#### Scenario: OTEL export endpoint
- **WHEN** the Langfuse callback is configured with `LANGFUSE_OTEL_HOST` set to a custom URL
- **THEN** the OTEL exporter sends traces to `{LANGFUSE_OTEL_HOST}/v1/traces`

#### Scenario: Default OTEL host
- **WHEN** `LANGFUSE_OTEL_HOST` is not set
- **THEN** the system defaults to `https://cloud.langfuse.com`

### Requirement: Trace-level input and output
The system SHALL set `langfuse.trace.input` and `langfuse.trace.output` span attributes on each LLM call so that Langfuse displays input/output at the trace level.

#### Scenario: Trace input from user message
- **WHEN** an LLM call includes messages with a user role
- **THEN** the system sets `langfuse.trace.input` to the last user message content

#### Scenario: Trace output from response
- **WHEN** an LLM call returns a response with content in the first choice
- **THEN** the system sets `langfuse.trace.output` to that content

### Requirement: Trace metadata injection
The system SHALL pass a `metadata` dict on each `litellm.acompletion()` call containing job attribution fields for Langfuse session grouping.

#### Scenario: Metadata fields on LLM calls
- **WHEN** `_agent_loop_generic()` makes an `acompletion()` call for a job
- **THEN** the call includes `metadata` with keys: `trace_name` (value: agent role), `session_id` (value: job_id), `trace_user_id` (value: agent_id), and `tags` (value: list containing "minion-suite")

#### Scenario: Metadata when no job context
- **WHEN** an LLM call is made without a job context (e.g., ad-hoc review)
- **THEN** the `session_id` and `trace_user_id` fields are empty strings

### Requirement: Config dataclass fields
The `Config` dataclass SHALL include `langfuse_public_key`, `langfuse_secret_key`, and `langfuse_host` fields, populated from environment variables `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, and `LANGFUSE_OTEL_HOST` respectively.

#### Scenario: Config loading
- **WHEN** `Config.from_env()` is called with Langfuse env vars set
- **THEN** the config object contains the corresponding field values

#### Scenario: Config defaults
- **WHEN** Langfuse env vars are not set
- **THEN** `langfuse_public_key` and `langfuse_secret_key` default to empty string, and `langfuse_host` defaults to `https://cloud.langfuse.com`

### Requirement: Preflight check
The system SHALL include an optional (warn-only) preflight check for Langfuse connectivity when Langfuse is configured.

#### Scenario: Langfuse reachable
- **WHEN** Langfuse is configured and the OTEL endpoint responds
- **THEN** the preflight check reports PASS with the endpoint URL

#### Scenario: Langfuse unreachable
- **WHEN** Langfuse is configured but the OTEL endpoint does not respond
- **THEN** the preflight check reports WARN (not FAIL) with an error message

#### Scenario: Langfuse not configured
- **WHEN** `LANGFUSE_PUBLIC_KEY` is empty
- **THEN** the preflight check reports WARN with "not configured"

### Requirement: Docker and deployment configuration
The system SHALL pass Langfuse environment variables through `docker-compose.yml` and document them in `.env.example`.

#### Scenario: Docker env passthrough
- **WHEN** the Docker Compose stack is started with Langfuse env vars in `.env`
- **THEN** the minion-suite container receives `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, and `LANGFUSE_OTEL_HOST`

### Requirement: Graceful initialization failure
The system SHALL catch and log any exception during Langfuse logger initialization and continue without tracing, rather than crashing.

#### Scenario: Missing OTEL packages
- **WHEN** Langfuse is configured but OTEL packages fail to import
- **THEN** the system logs a warning and continues without Langfuse tracing

#### Scenario: Invalid endpoint
- **WHEN** Langfuse is configured with an unreachable OTEL host
- **THEN** LLM calls proceed normally; failed span exports are silently dropped by the BatchSpanProcessor
