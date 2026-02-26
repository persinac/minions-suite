# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Minion Suite is a composable AI agent suite — vendor-agnostic (via LiteLLM), supporting GitLab/GitHub/Bitbucket. The first agent is a **code reviewer** with composable prompt profiles (role mixins + language mixins). Future agents (deploy monitor, test runner, etc.) will share the same infrastructure.

Requires **Python 3.14+**. Uses `uv` for dependency management and `task` (Taskfile) as the command runner.

## Architecture

**Data flow:** MR webhook / CLI → NATS `reviews.requested.<project>` → ReviewEngine → LiteLLM agent loop → Git provider API → inline comments + review verdict

**Review engine states:** `QUEUED → IN_PROGRESS → COMMENTED → DONE` (can transition to `FAILED`)

**Prompt composition:** `base.md` + role mixins (`roles/*.md`) + language mixins (`languages/*.md`) + custom rules (`custom/*.md`). Configured per-project in `projects.yaml`. Roles and languages can also be **auto-inferred** from changed file extensions/paths (e.g. `.py` → python, `/api/` → backend).

## Key Modules

- `cli.py` — Entry point: `minion review <url>`, `--server`, `--status`, `--costs`, `--preflight`
- `config.py` — Config dataclass loaded from environment (`Config.from_env()`)
- `agent.py` — LiteLLM tool-use loop; max 30 turns, 600s timeout; logs every turn to file
- `tools.py` — Tool definitions (OpenAI function schema) + `ToolExecutor` dispatch
- `server.py` — FastMCP server (port 8321) for external integrations
- `review_engine.py` — Polling loop: pick up queued reviews, run agent, publish results
- `prompt.py` — Composable prompt assembly from markdown mixins + auto-inference
- `git_provider.py` — `GitProviderProtocol` + GitLab (REST API v4) / GitHub (`gh` CLI) implementations
- `project_registry.py` — Multi-project config from `projects.yaml`
- `models.py` — Pydantic models: `Review`, `ReviewComment`, `Agent`; enums: `ReviewStatus`, `ReviewVerdict`, `GitProvider`
- `db.py` / `db_postgres.py` — `AbstractDatabase` protocol; SQLite (dev) and PostgreSQL (prod) implementations
- `preflight.py` — Health checks for CLI tools, API keys, git provider credentials, DB, NATS
- `connectors/nats_client.py` — Persistent NATS connection; subjects: `reviews.{requested,started,completed,failed}.<project>`

## Agent Tools

The LLM agent has access to these tools (defined in `tools.py`):
`get_mr_diff`, `get_changed_files`, `read_file` (with optional line range), `search_code` (ripgrep regex, 50 results max), `list_files` (glob), `get_mr_comments`, `post_inline_comment`, `submit_review`

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

Three services defined in `docker-compose.yml`:
- `minion-suite` — the app (built from `Dockerfile`); listens on `MCP_PORT` (8321)
- `postgres:17` — DB backend; schema initialised from `db/init.sql` on first start
- `nats:latest` — JetStream enabled; monitoring at `:8222`

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

There are no automated tests yet.

## Secrets

Never hardcode — use the secrets runner or env vars: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GITLAB_TOKEN`, `GH_TOKEN`
