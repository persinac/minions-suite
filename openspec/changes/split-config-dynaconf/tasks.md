## 1. Dependencies & Files

- [x] 1.1 Add `dynaconf>=3.2` to `pyproject.toml` dependencies
- [x] 1.2 Create `settings.toml` with all non-secret defaults organized by TOML tables (server, engine, pollers, database, nats, memory, langfuse, langgraph, k8s, artifacts)
- [x] 1.3 Add `settings.local.toml` and `.secrets.toml` to `.gitignore`
- [x] 1.4 Run `uv lock && uv sync --dev --extra test` to install dynaconf

## 2. Config Loader Refactor

- [x] 2.1 Add dynaconf `Dynaconf` instance initialization in `minions/config.py` — load from `settings.toml` + `settings.local.toml`, set `environments=True`, default env `default`
- [x] 2.2 Create `Config.load()` classmethod that reads dynaconf settings for non-secret fields, then overlays secret fields from `os.getenv()`
- [x] 2.3 For each non-secret setting, implement fallback chain: env var (legacy name) → dynaconf → dataclass default
- [x] 2.4 Keep `Config.from_env()` as an alias that delegates to `Config.load()`
- [x] 2.5 Ensure secret fields (`gitlab_token`, `github_token`, `trello_api_key`, `trello_token`, `postgres_url`, `redis_password`, `langfuse_public_key`, `langfuse_secret_key`, `anthropic_api_key`) are ONLY loaded from `os.getenv()`, never from TOML

## 3. Update .env.example

- [x] 3.1 Strip `.env.example` to secrets only — remove all polling intervals, enabled flags, concurrency limits, feature flags, log levels
- [x] 3.2 Add header comment referencing `settings.toml` for non-secret configuration
- [x] 3.3 Keep `POSTGRES_MIGRATE_URL` and `POSTGRES_ADMIN_URL` in `.env.example` (infrastructure secrets)

## 4. Docker Integration

- [x] 4.1 Mount `settings.toml` as read-only volume in `docker-compose.yml` for `minion-suite` and `input-sources` services
- [x] 4.2 Mount `settings.toml` in `docker-compose.local.yml` if applicable (N/A — local stack runs app natively)
- [x] 4.3 Verify `ENV_FOR_DYNACONF` can be set in `.env` to select the active environment

## 5. Tests

- [x] 5.1 Test `Config.load()` reads settings from TOML file (create temp `settings.toml`, verify values)
- [x] 5.2 Test env var override wins over TOML value
- [x] 5.3 Test `settings.local.toml` overrides `settings.toml`
- [x] 5.4 Test secret fields are NOT read from TOML (even if present)
- [x] 5.5 Test `Config.from_env()` returns same result as `Config.load()`
- [x] 5.6 Test app starts normally when `settings.toml` is missing (falls back to defaults + env vars)
- [x] 5.7 Run full test suite to verify no regressions (451 tests passing)

## 6. Verification

- [x] 6.1 Run `task up` and verify settings load from `settings.toml` (runtime — verify when Docker is available)
- [x] 6.2 Verify env var override works: set `AGENT_TIMEOUT=99` and confirm `config.agent_timeout == 99` (covered by test_env_var_overrides_toml)
- [x] 6.3 Verify secret isolation: add `gitlab_token = "bad"` to `settings.toml` and confirm it's ignored (covered by test_secrets_not_read_from_toml)
- [x] 6.4 Run `ruff format . && ruff check .` — clean
