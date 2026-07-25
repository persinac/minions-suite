# Minions Schema 

This repo houses all database related changes, migrations, etc.

# Running

  | Command                       | Description                                             |
  |-------------------------------|---------------------------------------------------------|
  | .\dbmate.ps1 pgsql up         | Apply all pending migrations                            |
  | .\dbmate.ps1 pgsql down       | Rollback the most recent migration                      |
  | .\dbmate.ps1 pgsql status     | Show migration status (applied vs pending)              |
  | .\dbmate.ps1 pgsql new <name> | Create a new migration file (e.g., new add_users_table) |
  | .\dbmate.ps1 pgsql migrate    | Alias for up                                            |
  | .\dbmate.ps1 pgsql rollback   | Alias for down                                          |
  | .\dbmate.ps1 pgsql create     | Create the database (if it doesn't exist)               |
  | .\dbmate.ps1 pgsql drop       | Drop the database                                       |
  | .\dbmate.ps1 pgsql dump       | Dump the schema to db/schema.sql                        |
  | .\dbmate.ps1 pgsql load       | Load schema from db/schema.sql                          |
  | .\dbmate.ps1 pgsql wait       | Wait for database to be available (useful in CI/Docker) |

  Most common workflow:
  # Check what needs to run
  .\dbmate.ps1 pgsql status

  # Apply pending migrations
  .\dbmate.ps1 pgsql up

  # Create a new migration
  .\dbmate.ps1 pgsql new add_some_feature

# Taskfile

### Install
```bash
scoop install task
```

# Credentials

`dbmate.sh` and `dbmate.ps1` resolve the connection in this order, first hit wins:

| Source | When it applies |
|---|---|
| `DATABASE_URL` | dbmate's own convention; `task db:migrate` sets it from `POSTGRES_MIGRATE_URL` |
| `POSTGRES_URL` | What the app, the `minion-suite-db` Secret and the Pulumi stack all use |
| `DB_ADMIN` / `DB_PASSWORD` / `DB_HOST` / `DB_PORT` / `DB_NAME` | A box that exports the parts |

`POSTGRES_URL` is the reason a pod needs no setup: the Secret already puts it in
the environment. It was previously ignored here, so the one credential sitting
in every running container was the one these scripts refused, and schema changes
had to be applied by hand — including hand-maintaining
`minions.schema_migrations` so dbmate's ledger did not drift.

When assembling from `DB_*` parts the SSL mode defaults to `disable`, matching
the local docker-compose Postgres. Note `config.py`'s `_build_postgres_url`
assembles `sslmode=require` from those same five variables, so pointing `DB_*`
at a managed instance works for the app and fails here unless you set
`DB_SSLMODE=require`. Against DO, prefer `POSTGRES_URL` — it already carries the
right mode.

# Running migrations in the cluster

```bash
task db:migrate:k8s          # apply pending migrations
task db:migrate:k8s:status   # show applied vs pending
```

Both exec into the running `minion-suite` pod and use its own `POSTGRES_URL`, so
the credential never leaves the cluster and nothing has to be exported, fetched
or printed locally. `dbmate` is baked into the image (pinned in the Dockerfile)
alongside `database/`, so no install step is needed there.

Override the namespace with `NS=<namespace>` if it is not `minion-suite`.
