# Minion Suite

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

Autonomous AI agent platform for software development — from issue to deployed code. Vendor-agnostic LLM support via [LiteLLM](https://github.com/BerriAI/litellm), multi-provider git integration (GitLab, GitHub), and optional Kubernetes dispatch for production workloads.

## What It Does

Minion Suite orchestrates specialized AI agents through the full development lifecycle:

1. **Ingest** — A GitLab issue, Trello card, MCP request, or CLI command triggers a job
2. **Plan** — A spec analyst refines requirements; an arbiter decomposes work into tasks per service
3. **Build** — Backend, frontend, and database engineers execute tasks: write code, run tests, commit, push branches, open PRs
4. **Review** — A code reviewer analyzes diffs, posts inline comments, and submits a verdict
5. **Revise** — If the reviewer requests changes, the engineer automatically reworks and resubmits
6. **Merge** — Approved PRs merge automatically (configurable per project)
7. **Deploy** — A deploy monitor watches CI/CD pipelines and verifies successful rollout

Each step is an independent agent with its own tools, operating within a state machine that handles retries, timeouts, and failure recovery.

### Agent Roles

| Role | What It Does |
|------|-------------|
| **Spec Analyst** | Refines raw feature requests into structured specs |
| **Arbiter** | Decomposes specs into tasks, assigns to services, coordinates multi-agent execution |
| **Backend Engineer** | Writes code, tests, commits, pushes branches, opens PRs |
| **Frontend Engineer** | Same as backend, scoped to frontend services |
| **Database Engineer** | Schema migrations, queries, data layer changes |
| **Code Reviewer** | Analyzes diffs, posts inline comments, approves or requests changes |
| **Deploy Monitor** | Watches CI/CD pipelines, verifies deployments |

### Standalone Features

- **GitLab Issues Poller** — Watches for labeled issues (`minions`), creates development jobs automatically
- **Trello Poller** — Converts cards from an "on-deck" list into jobs, syncs status back
- **MCP Server** — Exposes all operations (review, job submission, task control, cost queries) as MCP tools on port 8321
- **Web Dashboard** — Read-only UI showing active jobs, task progress, agent logs, and cost summaries

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

See [QUICKSTART.md](QUICKSTART.md) for detailed setup instructions.

### Local Docker Stack (Postgres + Redis + NATS)

Run infrastructure in Docker, app on the host:

```bash
# Start Postgres + Redis + NATS
task docker:local:up

# Initialize DB schema, roles, and NATS stream
task docker:local:init

# Fill in secrets in .env
# GITLAB_TOKEN=glpat-...
# ANTHROPIC_API_KEY=sk-ant-...

# Health checks
SECRETS_CMD="" task minion:preflight

# Start MCP server + job engine + pollers
SECRETS_CMD="" task minion:server
```

### Full Docker Stack

Production-like deployment with everything in Docker:

```bash
cp .env.example .env    # Fill in secrets
task docker:build
task docker:up
```

## Usage

### Review a Merge Request

```bash
# One-shot review (ad-hoc)
task minion:review -- https://gitlab.company.com/team/repo/-/merge_requests/42

# With a registered project (uses project-specific review profile)
task minion:review -- --project payments-api https://gitlab.company.com/team/repo/-/merge_requests/42

# Async — queue for the engine to pick up
task minion:review -- --async https://gitlab.company.com/team/repo/-/merge_requests/42

# GitHub
task minion:review -- https://github.com/org/repo/pull/99
```

### Submit a Development Job

```bash
# Via CLI
task minion:job -- "Add rate limiting to the /api/v2/export endpoint"

# Via GitLab issue — add the 'minions' label to any issue
# Via Trello — move a card to the 'minions-on-deck' list
# Via MCP — call the submit_spec tool on port 8321
```

### Run as a Service

```bash
# Full stack: MCP server + job engine + pollers + arbiter
task minion:server

# Check job status
task minion:status
task minion:costs -- --project payments-api
```

## Architecture

```
GitLab Issue / Trello Card / CLI / MCP / Webhook
                    |
                    v
               JobEngine              <- polls DB for active jobs
                    |
        +-----------+-----------+
        |                       |
        v                       v
     Review                Development
     Agent                 Orchestrator
        |                       |
        v                       v
   Code Review   Spec Analyst -> Arbiter -> Engineers -> Reviewer -> Deploy Monitor
        |                       |
        v                       v
   GitProvider   GitProvider + Shell + Filesystem
        |           |
        v           v
   Inline        PRs, merges,
   comments      deployments
```

### Job Lifecycle

**Review jobs:**

```
TASKS_CREATED -> REVIEW_IN_PROGRESS -> DONE
```

**Development jobs:**

```
SPEC_RECEIVED -> SPEC_READY -> TASKS_CREATED -> DEV_IN_PROGRESS -> PR_OPEN
    -> REVIEW_IN_PROGRESS -> MERGED -> DEPLOYING -> DEPLOYED -> DONE
```

Engineers execute one per service (sequential), but multiple services run in parallel. If a reviewer requests changes, the engineer automatically picks up a revision cycle before re-requesting review.

### Arbiter Coordination

When enabled (requires NATS), the arbiter provides:

- **State transition validation** — all status changes route through NATS request/reply
- **Heartbeat monitoring** — detects stale agents, triggers retries
- **Timeout enforcement** — per-role configurable limits
- **Anomaly detection** — rules-based detection of stuck tasks with automatic remediation
- **Circuit breaker** — temporary failure isolation under cascading errors

### Deployment Options

- **In-process** — agents run directly in the JobEngine process (development)
- **Kubernetes** — agents dispatched as isolated K8s pods with per-role resource limits, service accounts, and TTL cleanup (production)

## Project Configuration

Define projects in `projects.yaml` with composable profiles:

```yaml
defaults:
  model: claude-sonnet-4-6
  git_provider: gitlab

projects:
  payments-api:
    project_id: team/payments-api
    gitlab_url: https://gitlab.company.com
    auto_merge: false                        # Enterprise — review only, no auto-merge
    review_profile:
      roles: [backend, security]
      languages: [python, sql]
    engineer_profile:
      roles: [backend]
      languages: [python]
      timeout: 1800
    issues:
      enabled: true
      label: minions
    services:
      api:
        language: python
        framework: fastapi
        deploy_target: apprunner
        test_command: pytest
      worker:
        language: python
        deploy_target: k8s
    ignore_paths: ["*.lock", "alembic/versions/"]

  homelab-infra:
    project_id: alex/homelab
    auto_merge: true                         # Personal — merge on approve
    review_profile:
      roles: [devops]
      languages: [python, shell]
```

### Prompt Composition

Review prompts are built by layering markdown files:

| Layer | Directory | Purpose |
|-------|-----------|---------|
| Base | `prompts/base.md` | Universal review checklist (always loaded) |
| Roles | `prompts/roles/` | Domain expertise: `backend`, `frontend`, `data_engineer`, `devops`, `security` |
| Languages | `prompts/languages/` | Language rules: `python`, `typescript`, `go`, `sql`, `shell` |
| Custom | `prompts/custom/` | Org/team-specific rules (gitignored) |

If no profile is configured, roles and languages are auto-inferred from changed file paths.

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `LITELLM_MODEL` | No | `claude-opus-5` | LLM model string ([LiteLLM-supported](https://docs.litellm.ai/docs/providers)) |
| `ANTHROPIC_API_KEY` | \* | - | Anthropic API key |
| `OPENAI_API_KEY` | \* | - | OpenAI API key |
| `GITLAB_TOKEN` | \*\* | - | GitLab personal access token |
| `GITLAB_URL` | \*\* | - | GitLab instance URL |
| `GH_TOKEN` | \*\* | - | GitHub token |
| `POSTGRES_URL` | No | - | Postgres connection string |
| `NATS_ENABLED` | No | `false` | Enable NATS pub/sub |
| `NATS_SERVER_IP` | No | `localhost:4222` | NATS server address |
| `ARBITER_ENABLED` | No | `false` | Enable arbiter coordination |
| `GITLAB_ISSUES_ENABLED` | No | `false` | Enable GitLab issues poller |
| `GITLAB_ISSUES_POLL_INTERVAL` | No | `120` | Issues poll interval (seconds) |
| `PROJECTS_FILE` | No | `projects.yaml` | Path to projects config |
| `SECRETS_CMD` | No | `` | Secrets injection command prefix |
| `MAX_CONCURRENT_REVIEWS` | No | `3` | Max parallel reviews |
| `MAX_CONCURRENT_JOBS` | No | `3` | Max parallel jobs |
| `AGENT_TIMEOUT` | No | `600` | Default agent timeout (seconds) |
| `LOG_LEVEL` | No | `INFO` | Logging level |

\* At least one LLM API key is required.
\*\* At least one git provider token is required.

### Secrets Runner

Task commands support a configurable `SECRETS_CMD` prefix:

```bash
# Doppler
export SECRETS_CMD="doppler run --"

# AWS Secrets Manager
export SECRETS_CMD="./scripts/aws-sm-run minion/prod --"

# No wrapper (env vars already loaded / using .env file)
export SECRETS_CMD=""
```

## Task Commands

```bash
# Setup
task setup:init              # First-time setup (Python 3.14 + deps + preflight)
task setup:uv-all            # Re-sync after pyproject.toml changes

# Operations
task minion:review           # One-shot MR/PR review
task minion:server           # Full stack: MCP + engine + pollers + arbiter
task minion:preflight        # Health checks (APIs, DB, NATS, providers)
task minion:status           # Recent job history
task minion:costs            # Cost summary
task minion:kill             # Kill a stuck job by ID

# Code quality
task fmt                     # Format + fix lint (ruff)
task lint                    # Check formatting + lint (ruff)
task test                    # Run pytest, both suites (~1217 tests)
task e2e:hermetic            # Deterministic end-to-end suite (no network, no tokens)
task e2e:tickets             # List the ambiguous tickets for a live run
task e2e:live -- <ticket>    # Run one ambiguous ticket against real models
task e2e:grade [-- JOB_ID]   # Grade a live run's stated assumptions
                             #   needs Postgres+pgvector on :5434, not SQLite
                             #   see CLAUDE.md "Tests" for how to start one

# Docker — two stacks, mutually exclusive (they share ports 5434/6379/4222)
task docker:build            # Build Docker image
task docker:up / docker:down # Everything containerised (app + dashboard + pollers + infra)
task docker:local:up         # Infra only (postgres + redis + nats); app runs natively
task docker:local:init       # Initialize DB roles + NATS stream
task docker:local:reset      # Destroy infra volumes, start fresh, re-migrate
```

## NATS Subjects

When NATS is enabled, job lifecycle events are published:

| Subject | Description |
|---------|-------------|
| `jobs.review.requested.<project>` | Review job created |
| `jobs.review.started.<project>` | Review picked up by engine |
| `jobs.review.completed.<project>` | Review done (verdict + cost) |
| `jobs.review.failed.<project>` | Review agent errored |
| `jobs.<id>.status` | Per-job status updates |
| `agents.*` | Agent lifecycle events |
| `arbiter.state.transition` | State transition requests |
| `arbiter.heartbeat` | Agent liveness signals |

## MCP Server Tools

The MCP server (port 8321) exposes tools for external clients:

- **Review**: `request_review`, `get_review_status`, `get_review_history`, `cancel_review`
- **Jobs**: `submit_spec`, `submit_refined_spec`, `get_job_status`
- **Tasks**: `create_task`, `mark_tasks_created`, `update_task_status`, `report_pr`, `report_review_complete`, `report_deploy_status`
- **Subtasks**: `submit_subtask_plan`, `start_subtask`, `complete_subtask`, `fail_subtask`
- **Agents**: `send_heartbeat`, `send_message`, `get_messages`
- **Visibility**: `get_cost_summary`, `get_agent_logs`, `list_agent_logs`
- **Resources**: `job://{id}`, `job://active`, `agents://{job_id}`, `logs://{agent_id}`

## Database

**PostgreSQL only** — `psycopg3` async connection pool, configured via `POSTGRES_URL`.
Schema is applied by [dbmate](https://github.com/amacneil/dbmate) (`task db:migrate`);
pgvector is required.

Schema: jobs, tasks, subtasks, agents, messages, events, tool calls, heartbeats, state transitions

## Tests

Tests run via `pytest` + `pytest-asyncio`, split across two suites (root `tests/`
and `agent-memory/tests`). `task test` runs both.

**They need a real PostgreSQL with pgvector on `:5434`** — not SQLite. Without it,
DB-backed tests *error* at fixture setup rather than failing, which is easy to
misread as a broken change. `task up` provides a suitable container; see
[CLAUDE.md](CLAUDE.md#tests) for a standalone one.

```bash
task test
# or, root suite only
uv run pytest
```

## License

[Apache 2.0](LICENSE)
