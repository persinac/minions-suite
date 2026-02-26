# CLAUDE.md

## Project Overview

Minion Suite is a composable AI agent suite — vendor-agnostic (via LiteLLM), supporting GitLab/GitHub/Bitbucket. The first agent is a **code reviewer** with composable prompt profiles (role mixins + language mixins). Future agents (deploy monitor, test runner, etc.) will share the same infrastructure.

## Architecture

**Data flow:** MR webhook / CLI → NATS `reviews.requested.<project>` → ReviewEngine → LiteLLM agent loop → Git provider API → inline comments + review verdict

**Review engine states:** `QUEUED → IN_PROGRESS → COMMENTED → DONE` (can transition to `FAILED`)

**Prompt composition:** `base.md` + role mixins (`roles/*.md`) + language mixins (`languages/*.md`) + custom rules (`custom/*.md`). Configured per-project in `projects.yaml`.

## Key Modules

- `cli.py` — Entry point: `minion review <url>`, `--server`, `--status`, `--costs`, `--preflight`
- `config.py` — Config dataclass loaded from environment
- `agent.py` — LiteLLM tool-use loop (vendor-agnostic)
- `tools.py` — Tool definitions + execution dispatch
- `server.py` — FastMCP server for external integrations
- `review_engine.py` — Polling loop: pick up queued reviews, run agent, publish results
- `prompt.py` — Composable prompt assembly from markdown mixins
- `git_provider.py` — Protocol + GitLab/GitHub implementations
- `project_registry.py` — Multi-project config from projects.yaml
- `db.py` / `db_postgres.py` — Review history, agents, costs
- `connectors/nats_client.py` — Persistent NATS connection

## Coding Conventions

- No ternary/inline conditionals — use explicit if/else
- Atomic functions, prefer functional programming
- Use `logging` (not print)
- Use async/await for all I/O
- Type inbound/outbound interfaces; less strict internally
- `ruff format .` (line-length=150), `ruff check --fix .`

## Commands

```bash
# First-time setup
task setup:init

# Sync deps after pyproject.toml changes
task setup:uv-all

# One-shot review
task minion:review -- https://gitlab.company.com/team/repo/-/merge_requests/42
task minion:review -- --project payments-api https://gitlab.company.com/team/repo/-/merge_requests/42

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

# Docker
task docker:build
task docker:up
task docker:down
```

## Secrets

Never hardcode — use Doppler or env vars: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GITLAB_TOKEN`, `GH_TOKEN`
