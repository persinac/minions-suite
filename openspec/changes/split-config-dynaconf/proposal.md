## Why

The current `Config` dataclass loads everything from environment variables via a single `.env` file — API keys, database passwords, polling intervals, feature flags, and concurrency limits all live side-by-side. This creates two problems: (1) tuning a polling interval requires editing a file that contains production secrets, and (2) there's no layered override (e.g., base defaults → environment-specific settings → local overrides) without manually juggling env vars. Splitting secrets from settings makes the config safer to manage and easier to tune across environments.

## What Changes

- **Add dynaconf** as the configuration backend for non-secret settings (polling intervals, enabled flags, concurrency limits, feature flags, log levels, dispatch modes).
- **Keep `.env`** exclusively for actual secrets and API keys (`ANTHROPIC_API_KEY`, `GITLAB_TOKEN`, `GH_TOKEN`, `TRELLO_API_KEY`, `TRELLO_TOKEN`, `POSTGRES_URL`, `REDIS_PASSWORD`, `LANGFUSE_SECRET_KEY`, `PG_APP_PASSWORD`, etc.).
- **Create `settings.toml`** as the primary config file with sensible defaults, organized by section (server, engine, pollers, memory, langfuse, langgraph, k8s).
- **Create `settings.local.toml`** (gitignored) for local overrides — replaces the need for `.env.local` for non-secret tuning.
- **Support environment layering** via dynaconf environments (`[default]`, `[development]`, `[production]`) so settings can vary per deployment without file duplication.
- **Refactor `Config.from_env()`** to load from dynaconf first, then overlay secrets from env vars. The `Config` dataclass interface stays the same — all downstream code is unchanged.
- **Update `.env.example`** to contain only secrets, with a comment pointing to `settings.toml` for everything else.
- **BREAKING**: `.env` will no longer be the source of truth for non-secret settings. Existing `.env` files will still work during a transition period (dynaconf falls back to env vars), but `settings.toml` takes precedence when both are set.

## Capabilities

### New Capabilities
- `dynaconf-config`: Layered configuration via dynaconf — `settings.toml` for defaults, `settings.local.toml` for overrides, `.env` for secrets only. Includes environment support (default/development/production).

### Modified Capabilities

## Impact

- **`minions/config.py`** — Major refactor: `Config.from_env()` becomes `Config.load()`, reads dynaconf settings then overlays env-var secrets.
- **`pyproject.toml`** — Add `dynaconf` dependency.
- **`settings.toml`** — New file (checked in) with all non-secret defaults.
- **`settings.local.toml`** — New file (gitignored) for local overrides.
- **`.env.example`** — Stripped to secrets only, with reference to `settings.toml`.
- **`.gitignore`** — Add `settings.local.toml`, `.secrets.toml`.
- **`docker-compose*.yml`** — Mount `settings.toml` into containers.
- **`Taskfile.yml`** — No changes needed (reads from env, which still works).
- **All existing code** — No changes. `Config` dataclass interface is preserved.
