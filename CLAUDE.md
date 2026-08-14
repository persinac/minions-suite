# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Minion Suite is a composable AI agent suite — vendor-agnostic (via LiteLLM), supporting GitLab/GitHub/Bitbucket. The first agent is a **code reviewer** with composable prompt profiles (role mixins + language mixins). Future agents (deploy monitor, test runner, etc.) will share the same infrastructure.

Requires **Python 3.14+**. Uses `uv` for dependency management and `task` (Taskfile) as the command runner.

## Architecture

**Data flow:** MR webhook / CLI → `db.create_review_job()` → JobEngine → `run_agent()` (LiteLLM tool-use loop) → Git provider API → inline comments + review verdict

**Review job states:** A code review is a review-type Job with a single CODE_REVIEWER Task:
- Job: `TASKS_CREATED → REVIEW_IN_PROGRESS → DONE` (or `FAILED`)
- Task: `PENDING → IN_PROGRESS → DONE` (or `FAILED`)

**Development job states:** Multi-agent orchestration:
- Job: `SPEC_RECEIVED → SPEC_READY → TASKS_CREATED → DEV_IN_PROGRESS → PR_OPEN → REVIEW_IN_PROGRESS → MERGED → DEPLOYING → DEPLOYED → DONE`

**Prompt composition:** `base.md` + role mixins (`roles/*.md`) + language mixins (`languages/*.md`) + custom rules (`custom/*.md`). Configured per-project in `projects.yaml`. Roles and languages can also be **auto-inferred** from changed file extensions/paths (e.g. `.py` → python, `/api/` → backend).

## Package Structure

```
minions/
├── cli.py                          # Entry point: minion review/--server/--status/--costs/--preflight
├── config.py                       # Config dataclass loaded from environment (Config.from_env())
├── project_registry.py             # Multi-project config from projects.yaml
├── preflight.py                    # Health checks for CLI tools, API keys, providers, DB, NATS
├── dashboard.py                    # Web UI (single file)
├── artifact_uploader.py            # S3 artifact uploads
│
├── core/                           # Domain model — zero external deps
│   ├── models.py                   # Pydantic models (Job, Task, Agent, Subtask, Message) + enums
│   ├── state_transitions.py        # Transition validation, InvalidTransitionError
│   └── timeout_config.py           # RoleTimeoutConfig, TimeoutConfig
│
├── db/                             # Persistence layer
│   ├── abstract.py                 # AbstractDatabase protocol
│   ├── sqlite.py                   # SQLite implementation (dev, aiosqlite)
│   └── postgres.py                 # PostgreSQL implementation (prod, psycopg3)
│
├── engine/                         # Job orchestration
│   ├── job_engine.py               # Core JobEngine: poll loop, K8s dispatch, startup recovery
│   ├── job_graph.py                # LangGraph StateGraph for job orchestration (USE_LANGGRAPH_ENGINE)
│   ├── checkpointer.py             # LangGraph checkpointer factory (Postgres/SQLite)
│   ├── review.py                   # Review job handlers (launch, run in-process, check completion)
│   ├── dev.py                      # Dev handlers (spec analyst, arbiter, engineers, revisions)
│   ├── deploy.py                   # Deploy monitor launch, deployment checks
│   ├── arbiter.py                  # Arbiter coordination service (NATS request/reply)
│   └── anomaly_rules.py            # Anomaly detection rules for arbiter monitor loop
│
├── agents/                         # Agent execution
│   ├── runner.py                   # run_agent(), _agent_loop_generic() — unified LiteLLM tool-use loop
│   ├── graph.py                    # LangGraph subgraph wrapper for agent loop (USE_LANGGRAPH_AGENT)
│   ├── dispatch.py                 # AgentWorkItem, serialization (K8s handoff)
│   ├── prompt.py                   # build_prompt(), build_agent_prompt(), profile inference
│   └── tools/                      # Tool schemas + executors
│       ├── definitions.py          # OpenAI function schemas per role + get_tools_for_role()
│       ├── review_executor.py      # ToolExecutor (git provider dispatch for reviewers)
│       └── mcp_executor.py         # McpToolExecutor (MCP server routing for orchestration agents)
│
├── providers/                      # External service integrations
│   ├── git.py                      # GitProviderProtocol + GitLab/GitHub implementations
│   ├── trello.py                   # TrelloPoller
│   ├── gitlab_issues.py            # GitLabIssuesPoller
│   └── k8s.py                      # K8sJobLauncher
│
├── server/                         # MCP server
│   ├── mcp.py                      # FastMCP tool registrations (port 8321)
│   └── middleware.py               # ToolAuditMiddleware
│
├── renovate/                       # Renovate auto-merge (self-contained feature)
│   ├── classifier.py               # Risk classification, version parsing
│   └── engine.py                   # RenovateEngine polling loop
│
└── connectors/                     # NATS message bus
    └── nats_client.py              # Persistent NATS connection + publisher/subscriber
```

## Agent Tools

Each agent role has its own tool set (defined in `agents/tools/definitions.py`):
- **CODE_REVIEWER**: `get_mr_diff`, `get_changed_files`, `read_file`, `search_code`, `list_files`, `get_mr_comments`, `post_inline_comment`, `submit_review`, `report_review_complete`
- **Engineers** (backend/frontend): subtask tools + state tools + local tools (read/write/git/shell)
- **DATABASE_ENGINEER**: same as engineers but without subtask tools
- **DEPLOY_MONITOR**: subtask tools + send_message + state tools
- **SPEC_ANALYST / ARBITER**: spec submission + task creation + messaging tools

## Git Provider Notes

- **GitLab** — Uses REST API v4 directly; true inline comments via discussions API with version SHAs
- **GitHub** — Uses `gh` CLI subprocess; `post_inline_comment` falls back to regular PR comment (no true inline support)

## Database Backends

- **SQLite** (default, dev) — `aiosqlite`; file path from `DB_PATH` env var
- **PostgreSQL** (prod) — `psycopg3` async connection pool; set `DB_BACKEND=postgres` and `POSTGRES_URL`

## Coding Conventions

- No ternary/inline conditionals — use explicit if/else
- Atomic functions, prefer functional programming
- Use `logging` (not print)
- Use async/await for all I/O
- Type inbound/outbound interfaces; less strict internally
- `ruff format .` (line-length=150), `ruff check --fix .`

## Docker Stack

There are two bases. Pick by where the *app* runs:

**`docker-compose.dev.yml` — infra only, app runs natively.** Used by `task up` / `task down`. Postgres (`pgvector/pgvector:pg17`, `:5434`), Redis (`:6379`), NATS (`:4222`, monitor `:8222`). No app container, so you get live reload and a debugger. This is the everyday loop.

**`docker-compose.yml` — everything containerised.** Used by `task docker:up`. Adds `minion-suite` (built from `Dockerfile`, `MCP_PORT` 8321) and `input-sources` on top of the same infra. Requires `.env` (copy from `.env.example`).

Layered on top of either:
- `docker-compose.local.yml` — **gitignored**, optional, yours. `task up` includes it automatically if present, so personal overrides never become tree dirt. Don't edit `docker-compose.dev.yml` to change a port — override it here.
- `docker-compose.langfuse.yml` — `task up LANGFUSE=true`, or layered on `docker-compose.yml` manually. Note `LANGFUSE_OTEL_HOST` differs by path (`http://langfuse:3000` containerised, `http://localhost:3000` native) — see that file's header.

Schema is applied by **dbmate** (`task db:migrate`, migrations in `database/pgsql/migrations/`), not by an init script.

Apache AGE is *not* used — dropped 2026-07-25 in `20260314120000_add_memory_extensions.sql` (no runtime code path needs it; the managed target can't offer it). pgvector is required.

Copy `.env.example` → `.env` and fill in secrets before running `task docker:up`.

## Secrets Runner

The task commands use a configurable `SECRETS_CMD` env var (default: `doppler run --`).
Set it before running any `task minion:*` command to swap providers:

```bash
# AWS Secrets Manager (secrets stored as flat JSON objects)
export SECRETS_CMD="./scripts/aws-sm-run minion/prod --"

# No wrapper (env vars already in shell / docker-compose env_file)
export SECRETS_CMD=""
```

`scripts/aws-sm-run` is a self-contained uv script (manages its own `boto3` dep via PEP 723 inline metadata). It accepts one or more secret names before `--` and `os.execvpe`s the command with secrets merged into the environment — identical behaviour to `doppler run --`.

## Commands

```bash
# First-time setup
task setup:init

# Sync deps after pyproject.toml changes
task setup:uv-all

# One-shot review
task minion:review -- https://gitlab.yourcompany.com/team/repo/-/merge_requests/42
task minion:review -- --project payments-api https://gitlab.yourcompany.com/team/repo/-/merge_requests/42

# Start MCP server + review engine
task minion:server

# Health checks
task minion:preflight

# Review history / costs
task minion:status
task minion:costs -- --project payments-api

# Code formatting
task fmt
task lint

# Docker (requires .env)
task docker:build
task docker:up
task docker:down
```

## Tests

~1150 tests via `pytest` + `pytest-asyncio`, split across two suites. Run both with `task test` — it invokes pytest twice (root `tests/`, then `agent-memory/tests`) because both directories are named `tests` and their conftests collide in a single run. A bare `uv run pytest` runs only the root suite.

**Tests need a real PostgreSQL with pgvector — not SQLite.** `tests/conftest.py` connects to `postgresql://minion:minion@localhost:5434/minion` (override with `TEST_POSTGRES_URL`) and creates a `minions_test` schema per session. Without it, every DB-backed test *errors* at fixture setup rather than failing — easy to misread as "my change broke the suite".

pgvector specifically is required: the test schema declares `public.vector(1536)`, so plain `postgres:17` fails with `type "public.vector" does not exist`.

**If you already run `task up`, you have it** — `docker-compose.dev.yml`'s postgres is the same image, port, and credentials the tests expect, so no second container is needed.

Otherwise, a standalone one is enough:

```bash
docker run -d --name minion-test-pg \
  -e POSTGRES_USER=minion -e POSTGRES_PASSWORD=minion -e POSTGRES_DB=minion \
  -p 5434:5432 pgvector/pgvector:pg17
docker exec minion-test-pg psql -U minion -d minion -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

Run one or the other, not both — they collide on `:5434`.

## Secrets

Never hardcode — use the secrets runner or env vars: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GITLAB_TOKEN`, `GH_TOKEN`
