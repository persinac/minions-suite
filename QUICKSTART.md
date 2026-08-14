# Quickstart — Local Development

Get the full Minion Suite stack running locally in ~5 minutes.

## Prerequisites

Install these before starting:

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (with Docker Compose)
- [Task](https://taskfile.dev/installation/) — `brew install go-task`
- [uv](https://docs.astral.sh/uv/getting-started/installation/) — `brew install uv`
- A GitLab personal access token with API scope
- An Anthropic API key (or OpenAI, etc.)

## 1. Clone and configure

```bash
git clone <repo-url> && cd minions-suite

# Copy the example env and fill in your secrets
cp .env.example .env
```

Edit `.env` and fill in at minimum:

```
ANTHROPIC_API_KEY=sk-ant-...
GITLAB_TOKEN=glpat-...
GITLAB_URL=https://gitlab.com
```

The defaults for Postgres, NATS, and MCP ports are fine for local dev.

## 2. Configure a project

Create `projects.local.yaml` with at least one project:

```yaml
defaults:
  model: claude-sonnet-4-6
  git_provider: gitlab

projects:
  my-project:
    project_id: my-group/my-repo          # GitLab namespace/project path
    gitlab_url: https://gitlab.com
    review_profile:
      roles: [backend]
      languages: [python]
    issues:
      enabled: true
      label: minions                      # GitLab issues with this label get picked up
```

## 3. Start the stack

```bash
# Builds all containers, starts Postgres + Redis + NATS + app + dashboard.
task docker:up

# Apply the schema (dbmate).
task db:migrate
```

`docker:up` builds the image, then brings up every service in
`docker-compose.yml` and waits for the Postgres and Redis health checks before
starting the app containers.

Schema is a separate step because it is applied by dbmate against a running
database, not baked into the image.

Once complete you should see:

| Service | URL |
|---------|-----|
| MCP server | http://localhost:8321 |
| Dashboard | http://localhost:8322 |
| Postgres | localhost:5434 |
| NATS monitoring | http://localhost:8222 |

## 4. Verify it's running

```bash
# Tail the app logs
task docker:logs
```

You should see:

```
minion-suite  | GitLab issues poller started -- 1 project(s) with issues enabled
minion-suite  | Job engine started (poll interval: 5s, k8s_dispatch=False)
minion-suite  | Uvicorn running on http://0.0.0.0:8321
```

## 5. Trigger a job

On any GitLab issue in your configured project, add the trigger label (default: `minions`). Within the poll interval (default: 120 seconds), the poller will:

1. Pick up the issue
2. Swap the label to `minion-in-progress`
3. Post a comment with the job ID
4. Run the spec analyst → arbiter → engineer pipeline
5. On completion, swap the label to `minion-done` and close the issue

You can also trigger a one-shot code review without the poller:

```bash
SECRETS_CMD="" task minion:review -- https://gitlab.com/my-group/my-repo/-/merge_requests/42
```

## Common tasks

```bash
# Restart the app container (e.g., after label changes on an issue)
docker compose restart minion-suite

# Run DB migrations after pulling new code
task db:migrate

# Check migration status
task db:migrate:status

# Full reset (destroys data, rebuilds, re-migrates)
docker compose down -v && task docker:up && task db:migrate

# Stop everything
task docker:down

# Query the DB
docker compose exec postgres \
  psql -U minion -d minion -c "SELECT id, status, external_id FROM minions.jobs;"
```

> `task docker:local:*` is a *different* stack — infra only, with the app run
> natively on the host. Mixing the two collides on ports 5434/6379/4222. Pick one.

## DB migrations

Schema is managed by [dbmate](https://github.com/amacneil/dbmate). Migrations live in `database/pgsql/migrations/`.

```bash
task db:migrate          # Apply pending migrations
task db:migrate:status   # Show which migrations have run
task db:migrate:down     # Roll back the last migration
```

dbmate is auto-installed via Homebrew on first run if not already present.

Migrations connect using `POSTGRES_MIGRATE_URL` from `.env` (defaults to `localhost:5434` for the local Docker stack).

## Troubleshooting

**Poller not picking up issues?**
- Check the issue has the correct trigger label (default: `minions`, not `minion-in-progress`)
- The poll interval is 120s by default — wait or restart the container
- Check logs: `task docker:logs`

**Migration fails with "column already exists"?**
- `docker compose down -v && task docker:up && task db:migrate` for a clean slate. Nothing pre-creates tables — dbmate owns every one of them, so a wiped volume plus a migrate is always a valid starting state.

**"SSL is not enabled" error on migrate?**
- Ensure `POSTGRES_MIGRATE_URL` in `.env` ends with `?sslmode=disable`

**Container fails to start?**
- Check postgres logs: `docker compose logs postgres`
- Ensure port 5434 isn't already in use — `task up`, `task docker:up`, and a leftover `minion-test-pg` from the test setup all want it
