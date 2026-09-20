# Operator setup — standing this up on a second machine

`QUICKSTART.md` and `README.md` both describe **one machine running the whole
stack locally**: clone, `.env`, `task up`, a local Postgres. That is the
contributor path and it is complete.

This document covers the other thing you probably want, which neither of them
mentions: **a machine that drives the already-deployed system.** Cluster access,
the port-forward, the herder, the MCP client entry, and cutting a release.

If you are setting up a laptop to work *on* minion-suite, read `QUICKSTART.md`.
If you are setting up a laptop to work *with* the deployed minion-suite, read
this.

---

## Pick a mode first

The three modes have almost nothing in common, and mode A — the one most people
actually want — needs neither Docker nor a database.

| | **A. Client** | **B. Full dev** | **C. Release operator** |
|---|---|---|---|
| Run herder panes against prod | ✅ | ✅ | ✅ |
| Run the engine locally | ❌ | ✅ | ✅ |
| Run the test suite | ❌ | ✅ | ✅ |
| Build and push the image | ❌ | ❌ | ✅ |
| Needs kubectl | ✅ | optional | ✅ |
| Needs Docker | ❌ | ✅ | ✅ |
| Needs Doppler / secrets | ❌ | ✅ | ✅ |
| Needs AWS credentials | ❌ | ❌ | ✅ |

Modes are cumulative: C includes B includes A.

---

## What a clone gives you, and what it does not

This is the part that surprises people. `settings.toml` **is tracked** — it ships
with the clone, including values that are specific to the machine it was written
on. `projects.yaml` is **not**.

**Ships with the clone:**

- `settings.toml` — ⚠️ tracked, and carries machine-shaped values (`repo_base_dir`,
  `mcp_host`, `mcp_connect_host`). See hazard 1.
- `doppler.yaml` — only `doppler setup` reads it; it does not change what
  `doppler run` resolves.
- `.env.example`, `projects.example.yml` — templates.
- All of `scripts/`, including `mcp_forward.sh` and `scripts/systemd/`.

**Gitignored — you must recreate on the new machine:**

- `projects.yaml` (`.gitignore:21`) — copy from `projects.example.yml`.
- `.env` — copy from `.env.example`. Only needed for `task docker:up`.
- `docker-compose.local.yml` — optional personal overrides; `task up` layers it
  automatically if present.

**Not in the repo at all — set up by hand:**

- A kubectl context that can reach namespace `minion-suite`.
- The MCP server entry in `~/.claude.json`.
- Doppler auth (project `mcp-minions`, config `dev`), or the AWS Secrets Manager
  equivalent via `scripts/aws-sm-run`.

---

## Mode A — client only

You want this on a laptop that should run herder panes against the deployed
engine but should not host any of it.

### 1. Prerequisites

- `kubectl`, with a context for namespace `minion-suite`
- `uv` (`brew install uv`, or the Linux installer) — the trigger runs under it
- Claude Code, logged in on a subscription

No Docker, no Postgres, no Doppler. The engine, the database and the MCP server
all live in the cluster.

### 2. Clone

```bash
git clone <repo> ~/repos/personal/minions-suite
cd ~/repos/personal/minions-suite
```

⚠️ **Use that exact path if you intend to use the systemd units.** Both units
hardcode `WorkingDirectory=%h/repos/personal/minions-suite`. A clone anywhere
else works fine by hand but the units will fail to start. See hazard 3.

### 3. Hold the port-forward

The MCP service is ClusterIP with **no ingress**, so a laptop reaches it only
through a tunnel:

```bash
task herder:forward        # foreground; Ctrl-C to stop
```

This runs `scripts/mcp_forward.sh`, which supervises the forward rather than
just starting it — **a `kubectl port-forward` dies quietly** when its pod is
replaced, and keeps accepting connections that go nowhere. The script's periodic
`--check` actually speaks MCP and replaces a tunnel that is listening but not
answering. That check is why this is a script and not a one-liner.

Overridable: `MINIONS_MCP_SERVICE` (default `svc/minion-suite`),
`MINIONS_MCP_PORT` (default `8321`).

### 4. Point Claude Code at it

```bash
claude mcp add --scope user --transport http minions http://localhost:8321/mcp
```

That writes the top-level `mcpServers` entry in `~/.claude.json`, equivalent to:

```json
{
  "mcpServers": {
    "minions": {
      "type": "http",
      "url": "http://localhost:8321/mcp"
    }
  }
}
```

Use `--scope user` rather than the default `local`: `local` scopes the server to
the directory you happen to be in, and a herder pane spawned elsewhere will not
see it.

**On the transport:** since **0.8.66** the deployed server serves *both*
streamable HTTP on `/mcp` and SSE on `/sse`, simultaneously and by design — see
`minions/server/transport.py` for why the two apps must own disjoint paths.
Prefer `/mcp` on a new machine; it is the transport newer clients expect, and it
is what a client that has dropped SSE entirely (KiroCrew, for one) requires.

The older entry still works unchanged:

```json
{ "type": "sse", "url": "http://localhost:8321/sse" }
```

Because both are served at once, nothing about the switch has to be
coordinated — an existing pane on `/sse` and a new one on `/mcp` talk to the same
server. That is the whole point of dual-serving: a flip would have stranded every
herder pane already running, since `~/.claude.json` is read at session start.

⚠️ **`POST /sse` returns 405, and that is correct, not a fault.** SSE opens its
stream with `GET`; the 405 means the SSE app is mounted and answering. Do not read
it as a broken transport. The measurement that distinguishes deployed-vs-not is
`POST /mcp`: **404** before 0.8.66, **200** after.

MCP config is read **at session start**, so an already-running Claude Code
session keeps the old transport until it restarts.

### 5. Verify before trusting it

```bash
task herder:status         # mode, tunnel, queue. Changes nothing.
```

This is the honest health check: it reports whether the tunnel answers and what
the queue looks like, without claiming anything it did not measure.

### 6. Optional — run it unattended

```bash
task herder:install        # copies the units; does NOT enable spawning
```

Installing is deliberately not the same as enabling. From there:

```bash
systemctl --user enable --now minions-mcp-forward

systemctl --user edit minions-herder     # add:
#   [Service]
#   Environment=MINIONS_HERDER_MODE=live

systemctl --user enable --now minions-herder
loginctl enable-linger $USER             # survive logout/reboot
```

**`MINIONS_HERDER_MODE` defaults to `off`.** This is the single most important
property for a work laptop: the trigger will not spawn agents on a machine that
has not explicitly opted in, so pulling this repo onto a new host cannot turn it
into an agent spawner. Values are `off` (default) | `dry` | `live`. Set it in
`~/.tmux/env.sh` for the shell path, or in the unit override for the systemd
path.

Use `task herder:dry` to see exactly what it *would* spawn, spawning nothing.

```bash
task herder:logs           # journalctl -u minions-herder -f
```

---

## Mode B — full local development

Everything in mode A, plus a local stack.

### Prerequisites

- Python 3.14+, `uv`, `task` (`brew install go-task`), Docker
- A git provider token (`GITLAB_TOKEN` or `GH_TOKEN`)
- An LLM API key (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, …)
- Doppler CLI authed to project `mcp-minions`, **or** AWS SM via
  `scripts/aws-sm-run`

### Setup

```bash
task setup:init                       # pin Python, sync deps, run preflight
cp projects.example.yml projects.yaml # then edit
cp .env.example .env                  # only needed for task docker:up
task up                               # infra + native app processes
```

`task up` never destroys data. `task docker:local:reset` is the one that wipes
volumes.

### Secrets

`SECRETS_CMD` is declared **once**, in the root `Taskfile.yml`, and defaults to:

```
doppler run --project mcp-minions --config ${MINION_DOPPLER_CONFIG:-dev} --
```

Swap providers without editing anything:

```bash
SECRETS_CMD="./scripts/aws-sm-run minion/prod --" task minion:server
SECRETS_CMD="" task minion:server      # env already in the shell
MINION_DOPPLER_CONFIG=prd task minion:server
```

Note `${SECRETS_CMD-…}`, not `${SECRETS_CMD:-…}` — the colon form would
substitute on empty too, defeating the documented `SECRETS_CMD=""` opt-out.

### Tests

Tests need a **real Postgres with pgvector** on `:5434` — not SQLite, and not
plain `postgres:17` (the test schema declares `public.vector(1536)`). If you ran
`task up`, you already have it.

```bash
task test          # both suites: root tests/, then agent-memory/tests
```

Without a reachable Postgres, DB-backed tests *error at fixture setup* rather
than failing — easy to misread as "my change broke the suite."

---

## Mode C — cutting a release

There is **no CI that builds or pushes the image**. `.github/workflows/` holds
only the PR gates (`lint`, `test`, `secret-scan`). The release is a local task,
deliberately, so that it stays runnable by a human:

```bash
task docker:release
```

That runs, in order:

1. `scripts/check_deployed_schema.py` — **the schema gate, first, before
   anything is built.** 0.8.31 shipped code writing `jobs.original_spec` while
   the deployed database had no such column; every new development job died
   silently for forty minutes. The deploy verification that ran at the time
   confirmed the new *code* was live in the pod. Nobody asked whether the
   *schema* it depended on was. Override with `SKIP_SCHEMA_GATE=1` only when the
   release genuinely does not depend on pending migrations.
2. `scripts/bump-version.sh` — patch-bumps `VERSION`.
3. `task docker:ecr:push` — builds, tags from `VERSION`, pushes to ECR.
4. `scripts/ci-update-manifests.sh` — rewrites the image tag in the k8s overlays.

Then **you** commit and push:

```bash
git add VERSION k8s/ && git commit && git push
```

ArgoCD picks it up on its next poll. minion-suite ships **one image consumed by
three Deployments** (`minion-suite`, `minion-dashboard`, `input-sources`) through
a single `images:` entry in the prod overlay, so one tag moves all three.

### AWS prerequisites

Account `162756281464`, region `us-east-1`, profile `flashback-fleet`, repo
`minion-suite` — all four are defaults in `tasks/docker.yaml` and overridable as
task vars.

All three Deployments pull with `imagePullSecrets: [ecr-creds]`. A push to any
other registry therefore needs that secret to cover it, or the pods `ImagePullBackOff`
after the manifest commit lands — which looks like a bad image rather than a
credential scope.

The overlay pins an immutable `X.Y.Z` tag, **never `latest`**. `ecr:push` does also
push `:latest`, but as a convenience pointer only — nothing deployed reads it.
Rollback is therefore a manifest edit, not a rebuild.

### ⚠️ Verify the engine is idle before rolling

Agents run **in the engine pod** (`k8s_dispatch=False`), so "no agent pods" proves
nothing. A pod-template change rolls prod and can kill a running agent mid-job.
Check for live agents before you push the manifest commit — and check *again after
the build*, which takes long enough for work to arrive in the gap.

`minion-suite` is `strategy: Recreate`, so the old pod is terminated before the new
one starts. A snapshot taken during that window shows the new pod `Pending` with no
old pod beside it. That is the strategy working, not a stuck rollout — read the
events before concluding anything.

### ⚠️ Rolling prod kills your own MCP session

If you drive the release *from* a Claude Code session using the minions MCP tools,
the rollout replaces the pod your client is connected to. Every subsequent tool call
then fails — as `Invalid request parameters`, which looks like you called it wrong
rather than like the session died. The tunnel is fine and the server is fine; only
the client's session is stale. Restart the session to reconnect.

Before believing a tool failure means a broken deploy, probe past your client:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8321/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}'
```

`200` means the server is healthy and the problem is your client.

### Applying a migration to the deployed database

The deployed database is **firewalled to the cluster** — you cannot reach it from
a laptop. Use the task that runs dbmate inside the pod, so the credential never
leaves the cluster:

```bash
# 1. Copy the migration in — the running pod has the PREVIOUS image.
kubectl cp database/pgsql/migrations/<file>.sql \
  minion-suite/<pod>:/app/database/pgsql/migrations/<file>.sql
kubectl exec -n minion-suite <pod> -- sha256sum /app/database/pgsql/migrations/<file>.sql

# 2. Dry run, then apply. Note the override — the default is not what you want.
task db:migrate:k8s:status
task db:migrate:k8s DBMATE_CMD="--no-dump-schema migrate"
```

⚠️ **`DBMATE_CMD` defaults to `up`, and the task does no `kubectl cp`.** Both
defaults are wrong for this job, which is why step 1 is by hand and step 2 passes
an override. `task db:migrate:k8s` on its own runs `dbmate.sh pgsql up` against
whatever migrations the *running image* happens to contain.

Three traps, all of which have bitten:

1. **The running pod has the *previous* image**, so a migration you just merged
   is not in it. `dbmate status` then reports `Pending: 0` — a false green at
   exactly the wrong moment. Hence the `kubectl cp` above; `/app` is writable.
   Verify by `sha256sum` against the local file, not by eye.
2. **Pass `--no-dump-schema`.** Otherwise dbmate tries to write `./db/schema.sql`
   after migrating, fails on a path that does not exist, and exits non-zero
   *after the migration applied* — which reads as "it failed" when it did not.
3. **Use `migrate`, not the default `up`.** `up` creates the database first, and
   the managed role may not have `CREATEDB`. `migrate` only applies pending.

Verify with something that would differ if it had not worked:
`uv run python scripts/check_deployed_schema.py` (exit 0, and the migration count
went up). For a constraint, "it appears in `pg_indexes`" is the green-test trap —
actually attempt the thing it must refuse, inside a transaction you roll back.

Measure the blast radius before mutating: a non-concurrent `CREATE INDEX` takes
`ACCESS EXCLUSIVE`, which locks the engine out for as long as the table takes.

---

## Hazards

### 1. `settings.toml` is tracked and machine-shaped

It carries `repo_base_dir`, `mcp_host`, `mcp_connect_host` — paths and hosts that
are correct on the machine they were written on. Do **not** edit the tracked file
to suit a new laptop; you will carry that diff forever and eventually commit it.
Override via environment variables or `docker-compose.local.yml`, both of which
are gitignored.

### 2. ⚠️ `POSTGRES_URL` comes from the environment — which locally means the deployed database

`config.py:543` builds it via `_build_postgres_url()` and marks it
`# secret — always from env`. There is **no `postgres_url` key in
`settings.toml`** — the `[default.database]` section holds pool configuration
only. (An older note in `CLAUDE.md` attributes this to `settings.toml`; the
hazard is real, the mechanism named there is not.)

The practical consequence: under `doppler run`, a local e2e harness resolves the
**deployed** database. Always override for local runs:

```bash
POSTGRES_URL=postgresql://minion:minion@localhost:5434/minion task e2e:probe -- missing-bound
```

`database/dbmate.sh` resolves in the order `DATABASE_URL` → `POSTGRES_URL` →
assembled `DB_*` parts.

### 3. The systemd units hardcode the clone path

Both units set `WorkingDirectory=%h/repos/personal/minions-suite`. If you clone
elsewhere, either clone to that path or `systemctl --user edit` each unit to
correct it. A wrong path fails at start rather than silently, but the error does
not name the cause obviously.

### 4. Port 8321 is the tunnel to the deployed service

Never point a local MCP server at `:8321` while the forward is running. You will
shadow the tunnel and talk to the wrong thing without any error. If you want a
local server too, give it a different port.

### 5. The Docker stacks are mutually exclusive

`docker-compose.dev.yml` (infra only, `task up`) and `docker-compose.yml`
(everything containerised, `task docker:up`) publish the same host ports
(5434/6379/4222), as does a leftover `minion-test-pg` from the test setup. Run
one, not two.

---

## Quick reference

```bash
task herder:status       # what the trigger sees — mode, tunnel, queue
task herder:forward      # hold the port-forward (foreground)
task herder:dry          # one tick, prints the spawn it would make
task herder:watch        # poll and spawn (foreground)
task herder:install      # install systemd units (does not enable)
task herder:logs         # tail the journal

task up                  # infra + native app
task down                # stop everything
task test                # both suites
task lint                # check without modifying

task minion:preflight    # health checks
task minion:status       # review history
task minion:costs

task docker:schema:list  # local vs deployed migration versions
task docker:schema:check # fail if deployed is behind
task docker:release      # gate, bump, build, push, rewrite overlay

task db:migrate          # local
task db:migrate:k8s:status
```
