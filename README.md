# Minion Suite

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

Composable AI agent suite — vendor-agnostic (via [LiteLLM](https://github.com/BerriAI/litellm)), supporting GitLab, GitHub, and Bitbucket. Includes a **code reviewer**, a **multi-agent development orchestrator**, and a **GitLab issues poller** for autonomous job creation.

## Quickstart

### Prerequisites

- [uv](https://docs.astral.sh/uv/) — Python package manager
- [Task](https://taskfile.dev/) — task runner
- [Docker](https://www.docker.com/) — for local Postgres + NATS
- A git provider token (`GITLAB_TOKEN` or `GH_TOKEN`)
- An LLM API key (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, etc.)

### Setup

```bash
cd minion-suite

# Install Python 3.14, sync dependencies, run health checks
task setup:init
```

### Quickstart

View: [QUICKSTART.md](QUICKSTART.md)

### Local Docker Stack (Postgres + NATS)

For local development, run only the infrastructure services (Postgres and NATS) in Docker and the app on the host:

```bash
# Start Postgres + NATS
task docker:local:up

# Initialize DB schema, roles, and NATS stream
task docker:local:init

# Fill in secrets in .env
# GITLAB_TOKEN=glpat-...
# ANTHROPIC_API_KEY=sk-ant-...

# Health checks
SECRETS_CMD="" task minion:preflight

# Start MCP server + job engine + GitLab issues poller
SECRETS_CMD="" task minion:server
```

### Full Docker Stack

For production-like deployments with the app running in Docker alongside Postgres and NATS:

```bash
# Copy and fill in .env
cp .env.example .env

# Build and start all services
task docker:build
task docker:up
```

### Review a merge request

```bash
# One-shot review (ad-hoc — no project config needed)
task minion:review -- https://gitlab.company.com/team/repo/-/merge_requests/42

# Review with a registered project (uses project-specific review profile)
task minion:review -- --project payments-api https://gitlab.company.com/team/repo/-/merge_requests/42

# GitHub works too
task minion:review -- https://github.com/org/repo/pull/99
```

### Run as a service

```bash
# Start MCP server + job engine + pollers
task minion:server

# Check recent reviews
task minion:status

# Cost summary
task minion:costs
task minion:costs -- --project payments-api
```

## Architecture

```
Webhook / CLI / GitLab Issue / Trello Card
        |
        v
   JobEngine              <- polls DB for active jobs
        |
   +----+----+
   |         |
   v         v
 Review    Development
 Agent     Orchestrator   <- multi-agent: spec -> tasks -> engineer -> PR -> review -> merge
   |         |
   v         v
 Tools      Tools         <- get_diff, read_file, search_code, post_inline_comment, ...
   |         |
   v         v
 GitProvider              <- GitLab API / GitHub CLI (protocol-based)
   |
   v
 MR comments + verdict / PRs + merges
```

**Job types:**

- **Review jobs:** `TASKS_CREATED -> REVIEW_IN_PROGRESS -> DONE` (or `FAILED`)
- **Development jobs:** `SPEC_RECEIVED -> SPEC_READY -> TASKS_CREATED -> DEV_IN_PROGRESS -> PR_OPEN -> REVIEW_IN_PROGRESS -> MERGED -> DEPLOYING -> DEPLOYED -> DONE`

**GitLab Issues Poller:** Watches configured projects for issues with a trigger label (default: `minions`), creates development jobs automatically, and updates issue labels/comments as jobs progress.

**Arbiter:** Optional coordination service (requires NATS) that routes MCP tool state mutations for multi-agent conflict resolution.

## Project Configuration

Define projects in `projects.yaml` (or `projects.local.yaml` for local dev) with composable review profiles:

```yaml
defaults:
  model: claude-sonnet-4-6
  git_provider: gitlab

projects:
  payments-api:
    project_id: team/payments-api
    gitlab_url: https://gitlab.company.com
    review_profile:
      roles: [backend, security]
      languages: [python, sql]
    issues:
      enabled: true
      label: minions
    ignore_paths: ["*.lock", "alembic/versions/"]

  checkout-ui:
    project_id: team/checkout-ui
    gitlab_url: https://gitlab.company.com
    review_profile:
      roles: [frontend]
      languages: [typescript]
```

### Review Profiles

Prompts are composed by layering markdown files:

| Layer | Directory | Purpose |
|-------|-----------|---------|
| Base | `prompts/base.md` | Universal review checklist (always loaded) |
| Roles | `prompts/roles/` | Domain expertise: `backend`, `frontend`, `data_engineer`, `devops`, `security` |
| Languages | `prompts/languages/` | Language rules: `python`, `typescript`, `go`, `sql`, `shell` |
| Custom | `prompts/custom/` | Org/team-specific rules (gitignored) |

If no profile is configured, the reviewer auto-infers roles and languages from the changed file paths.

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `LITELLM_MODEL` | No | `gpt-4o` | LLM model string (any [LiteLLM-supported model](https://docs.litellm.ai/docs/providers)) |
| `ANTHROPIC_API_KEY` | \* | - | Anthropic API key |
| `OPENAI_API_KEY` | \* | - | OpenAI API key |
| `GITLAB_TOKEN` | \*\* | - | GitLab personal access token |
| `GITLAB_URL` | \*\* | - | GitLab instance URL |
| `GH_TOKEN` | \*\* | - | GitHub token |
| `DB_BACKEND` | No | `sqlite` | `sqlite` or `postgres` |
| `POSTGRES_URL` | No | - | Postgres connection string (when `DB_BACKEND=postgres`) |
| `NATS_ENABLED` | No | `false` | Enable NATS pub/sub for job events |
| `NATS_SERVER_IP` | No | `localhost:4222` | NATS server address |
| `GITLAB_ISSUES_ENABLED` | No | `false` | Enable GitLab issues poller |
| `GITLAB_ISSUES_POLL_INTERVAL` | No | `120` | Issues poll interval in seconds |
| `PROJECTS_FILE` | No | `projects.yaml` | Path to projects config YAML |
| `SECRETS_CMD` | No | `doppler run --` | Secrets injection command prefix (set empty for no wrapper) |
| `MAX_CONCURRENT_REVIEWS` | No | `3` | Max parallel reviews |
| `MAX_CONCURRENT_JOBS` | No | `3` | Max parallel jobs |
| `AGENT_TIMEOUT` | No | `600` | Agent timeout in seconds |
| `ARBITER_ENABLED` | No | `false` | Enable arbiter coordination service |
| `LOG_LEVEL` | No | `INFO` | Logging level |

\* At least one LLM API key is required.
\*\* At least one git provider token is required.

### Secrets Runner

Task commands use a configurable `SECRETS_CMD` env var (default: `doppler run --`). Set it before running any `task minion:*` command:

```bash
# Doppler (default)
export SECRETS_CMD="doppler run --"

# AWS Secrets Manager
export SECRETS_CMD="./scripts/aws-sm-run minion/prod --"

# No wrapper (env vars already in shell / .env file)
export SECRETS_CMD=""
```

## Task Commands

```bash
task setup:init          # First-time setup (Python 3.14 + deps + preflight)
task setup:uv-all        # Re-sync after pyproject.toml changes

task minion:preflight    # Health checks
task minion:review       # One-shot MR/PR review
task minion:server       # MCP server + job engine + pollers + arbiter
task minion:status       # Recent review history
task minion:costs        # Cost summary

task fmt                 # Format + fix lint (ruff)
task lint                # Check formatting + lint (ruff)

task docker:build        # Build Docker image
task docker:up           # Start full Docker stack
task docker:down         # Stop Docker stack

task docker:local:up     # Start local Postgres + NATS (no app container)
task docker:local:down   # Stop local Postgres + NATS
task docker:local:init   # Initialize local DB schema + NATS stream
task docker:local:reset  # Destroy volumes and restart fresh
task docker:local:logs   # Tail local infra logs
```

## NATS Subjects

When NATS is enabled, job lifecycle events are published:

| Subject | Description |
|---------|-------------|
| `jobs.review.requested.<project>` | Review job created |
| `jobs.review.started.<project>` | Review picked up by engine |
| `jobs.review.completed.<project>` | Review done (includes verdict + cost) |
| `jobs.review.failed.<project>` | Review agent errored |
| `jobs.<id>.status` | Per-job status updates |
| `agents.*` | Agent lifecycle events |

## License

[Apache 2.0](LICENSE)
