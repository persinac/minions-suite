# Minion Suite

Composable AI agent suite — vendor-agnostic (via [LiteLLM](https://github.com/BerriAI/litellm)), supporting GitLab, GitHub, and Bitbucket. Starting with a **code reviewer**, with more agents to follow.

## Quickstart

### Prerequisites

- [uv](https://docs.astral.sh/uv/) — Python package manager
- [Task](https://taskfile.dev/) — task runner
- [Doppler](https://www.doppler.com/) — secrets injection
- A git provider token (`GITLAB_TOKEN` or `GH_TOKEN`)
- An LLM API key (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, etc.)

### Setup

```bash
# Clone and enter the repo
cd minion-suite

# Install Python 3.14, sync dependencies, run health checks
task setup:init
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
# Start MCP server + review engine (polls for queued reviews)
task minion:server

# Check recent reviews
task minion:status

# Cost summary
task minion:costs
task minion:costs -- --project payments-api
```

## Project Configuration

Define projects in `projects.yaml` with composable review profiles:

```yaml
defaults:
  model: gpt-4o
  git_provider: gitlab

projects:
  payments-api:
    project_id: team/payments-api
    gitlab_url: https://gitlab.company.com
    repo_path: /repos/payments-api
    review_profile:
      roles: [backend, security]
      languages: [python, sql]
    ignore_paths: ["*.lock", "alembic/versions/"]

  checkout-ui:
    project_id: team/checkout-ui
    gitlab_url: https://gitlab.company.com
    review_profile:
      roles: [frontend]
      languages: [typescript]
    model: claude-sonnet-4-20250514
```

### Review Profiles

Prompts are composed by layering markdown files:

| Layer | Directory | Purpose |
|-------|-----------|---------|
| Base | `prompts/base.md` | Universal review checklist (always loaded) |
| Roles | `prompts/roles/` | Domain expertise: `backend`, `frontend`, `data_engineer`, `devops`, `security` |
| Languages | `prompts/languages/` | Language rules: `python`, `typescript`, `go`, `sql`, `shell` |
| Custom | `prompts/custom/` | Org/team-specific rules (gitignored) |

A project with `roles: [backend, security]` and `languages: [python, sql]` gets a prompt built from:

```
base.md → roles/backend.md → roles/security.md → languages/python.md → languages/sql.md
```

If no profile is configured, the reviewer auto-infers roles and languages from the changed file paths.

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `LITELLM_MODEL` | No | `gpt-4o` | LLM model string (any [LiteLLM-supported model](https://docs.litellm.ai/docs/providers)) |
| `OPENAI_API_KEY` | * | — | OpenAI API key |
| `ANTHROPIC_API_KEY` | * | — | Anthropic API key |
| `GITLAB_TOKEN` | ** | — | GitLab personal access token |
| `GITLAB_URL` | ** | — | GitLab instance URL |
| `GH_TOKEN` | ** | — | GitHub token |
| `DB_BACKEND` | No | `sqlite` | `sqlite` or `postgres` |
| `POSTGRES_URL` | No | — | Postgres connection string (when `DB_BACKEND=postgres`) |
| `NATS_ENABLED` | No | `false` | Enable NATS pub/sub for review events |
| `NATS_SERVER_IP` | No | `localhost:4222` | NATS server address |
| `MAX_CONCURRENT_REVIEWS` | No | `3` | Max parallel reviews in engine mode |
| `AGENT_TIMEOUT` | No | `600` | Agent timeout in seconds |

\* At least one LLM API key is required.
\** At least one git provider token is required.

## Task Commands

```bash
task setup:init          # First-time setup (Python 3.14 + deps + preflight)
task setup:uv-all        # Re-sync after pyproject.toml changes

task minion:preflight    # Health checks
task minion:review       # One-shot MR/PR review
task minion:server       # MCP server + review engine
task minion:status       # Recent review history
task minion:costs        # Cost summary
task minion:logs         # Tail agent logs

task fmt                 # Format + fix lint (ruff)
task lint                # Check formatting + lint (ruff)

task docker:build        # Build Docker image
task docker:up           # Start container
task docker:down         # Stop container
task docker:logs         # Tail container logs
```

## Architecture

```
MR URL / Webhook / NATS
        │
        ▼
   ReviewEngine          ← polls DB for QUEUED reviews
        │
        ▼
   agent.py              ← LiteLLM tool-use loop (vendor-agnostic)
   ┌────┴────┐
   │  Tools  │           ← get_diff, read_file, search_code, post_inline_comment, submit_review
   └────┬────┘
        │
        ▼
   GitProvider           ← GitLab API / GitHub CLI / Bitbucket (protocol-based)
        │
        ▼
   MR comments + verdict
```

**NATS subjects** (when enabled):
- `reviews.requested.<project>` — trigger a review
- `reviews.started.<project>` — engine picked it up
- `reviews.completed.<project>` — done (includes verdict + cost)
- `reviews.failed.<project>` — agent errored
