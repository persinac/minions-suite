## Context

Today `minions/config.py` has a single `Config` dataclass with 50+ fields, all loaded from `os.getenv()` in `Config.from_env()`. Every setting — from `AGENT_TIMEOUT=600` to `ANTHROPIC_API_KEY=sk-...` — lives in `.env`. This means:

- Tuning a polling interval requires editing a file containing production API keys.
- No layered defaults — every environment copies the entire `.env` and tweaks values.
- Taskfile, docker-compose, and the app all read the same flat namespace with no structure.
- Adding a new setting means adding an env var name, a default, and type coercion in `from_env()`.

The `Config` dataclass is used everywhere via `config = Config.from_env()`. All downstream code accesses `config.field_name` — this interface must not change.

## Goals / Non-Goals

**Goals:**
- Separate secrets (API keys, tokens, passwords, connection strings) from settings (intervals, flags, limits, modes).
- Use dynaconf to load settings from `settings.toml` with layered overrides (`settings.local.toml`).
- Support dynaconf environments (`[default]`, `[development]`, `[production]`) for per-deployment tuning.
- Keep the `Config` dataclass interface identical — zero changes to any consumer code.
- Maintain backward compatibility: existing env vars still work as a fallback so nothing breaks during migration.

**Non-Goals:**
- Splitting `Config` into multiple smaller config objects (one refactor at a time).
- Adding runtime config reload / hot-reload (dynaconf supports it, but we won't wire it up now).
- Moving secrets into a vault or secrets manager (that's the `SECRETS_CMD` runner's job).
- Changing the `projects.yaml` or prompt system — those are separate config domains.

## Decisions

### 1. dynaconf as the settings backend

**Choice:** Use dynaconf with TOML files.

**Why:** dynaconf is the de facto Python config library — supports TOML, environment layering, env var overrides (with `DYNACONF_` prefix), validators, and `.local.toml` convention out of the box. It's lightweight (no C deps) and well-maintained.

**Alternatives considered:**
- **pydantic-settings**: More type-safe but doesn't support TOML layering natively. Would require custom merge logic.
- **python-decouple**: Too simple — no layering, no environments, no TOML.
- **Manual TOML loading**: More control but reinvents env layering, override precedence, and type coercion.

### 2. Settings file structure

**Choice:** Single `settings.toml` with TOML tables matching config sections.

```toml
[default]
model = "gpt-4o"
log_level = "INFO"
dry_run = false

[default.server]
mcp_port = 8321
mcp_host = "localhost"

[default.engine]
poll_interval = 10
max_concurrent_reviews = 3
max_concurrent_jobs = 3
max_revisions = 3
agent_timeout = 600

[default.pollers.gitlab_issues]
enabled = false
poll_interval = 120

[default.pollers.trello]
poll_interval = 180

[default.pollers.renovate]
enabled = false
poll_interval = 60
max_concurrent = 2

[default.database]
backend = "sqlite"
db_path = "reviews.db"
pool_min = 2
pool_max = 10

[default.nats]
enabled = false
stream = "minions"

[default.memory]
enabled = false
l3_token_budget = 2000
log_level = "INFO"

[default.langfuse]
host = "https://cloud.langfuse.com"

[default.langgraph]
use_agent = true
use_engine = true

[default.k8s]
dispatch = false
namespace = "minion-suite"
agent_sa = "minion-suite-agent"
job_ttl = 3600

[default.artifacts]
region = "us-east-1"
prefix = "minions"

[development]
log_level = "DEBUG"

[development.database]
backend = "postgres"
```

**Why nested tables:** Groups related settings, mirrors the mental model ("what are the poller settings?"), and avoids flat-namespace collisions. dynaconf flattens these for env var override (`DYNACONF_ENGINE__POLL_INTERVAL=5`).

### 3. Secret loading stays via env vars

**Choice:** Secrets continue to load from `os.getenv()`. dynaconf's `.secrets.toml` is NOT used.

**Why:** The project already has a `SECRETS_CMD` runner pattern (doppler, AWS SM) that injects secrets as env vars. Adding `.secrets.toml` would create a second secret source and complicate the flow. Env vars are the universal interface between secret managers and the app.

**Secret fields (stay in `.env`):**
- `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`
- `GITLAB_TOKEN`, `GH_TOKEN`
- `TRELLO_API_KEY`, `TRELLO_TOKEN`
- `POSTGRES_URL`, `POSTGRES_ADMIN_URL`, `PG_APP_PASSWORD`, `DB_PASSWORD`
- `REDIS_PASSWORD`
- `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`
- `AWS_PROFILE_NAME` (not really a secret, but tied to AWS credential context)

### 4. Backward compatibility via env var fallback

**Choice:** `Config.load()` reads dynaconf first, then overlays env vars. For every non-secret setting, if an env var is set, it wins over the TOML value.

**Why:** Existing deployments have `.env` files with settings mixed in. This lets them work unchanged. Over time, operators move non-secret settings to `settings.toml` and slim down `.env`.

**Precedence order (highest wins):**
1. Explicit env var (e.g., `AGENT_TIMEOUT=300`)
2. `settings.local.toml` (gitignored, local overrides)
3. `settings.toml` (checked in, team defaults)
4. Dataclass defaults in `Config`

### 5. Config.from_env() preserved as alias

**Choice:** Keep `Config.from_env()` as an alias for `Config.load()`.

**Why:** 30+ call sites use `Config.from_env()`. Adding an alias costs nothing and avoids a noisy rename across the codebase.

## Risks / Trade-offs

- **[Risk] Env var name mapping complexity** — dynaconf uses `DYNACONF_` prefix for its env vars, but existing code uses unprefixed names (`AGENT_TIMEOUT`). → Mitigation: The fallback layer in `Config.load()` explicitly checks the legacy env var names. dynaconf env vars are a bonus, not required.

- **[Risk] Two sources of truth during migration** — Settings can come from both `.env` and `settings.toml`. → Mitigation: Env vars always win (explicit override), so there's no ambiguity about which value applies. Logged at startup which source each key came from (at DEBUG level).

- **[Risk] TOML file not present in existing deployments** — Docker images or CI that don't mount `settings.toml`. → Mitigation: All defaults are in the `Config` dataclass. dynaconf missing files is non-fatal. The app starts fine with just env vars.

- **[Trade-off] Additional dependency** — dynaconf adds ~1MB to the image. Acceptable given the value.
