## ADDED Requirements

### Requirement: Settings loaded from TOML via dynaconf
The system SHALL load non-secret configuration settings from `settings.toml` using dynaconf. The file SHALL use TOML tables to group related settings (server, engine, pollers, database, nats, memory, langfuse, langgraph, k8s, artifacts). dynaconf SHALL be initialized with `envvar_prefix=false` so that legacy unprefixed env vars continue to work as overrides.

#### Scenario: Application starts with settings.toml present
- **WHEN** `settings.toml` exists in the project root
- **THEN** all non-secret settings are loaded from it and applied to the `Config` dataclass

#### Scenario: Application starts without settings.toml
- **WHEN** `settings.toml` does not exist
- **THEN** the application starts normally using `Config` dataclass defaults and any env var overrides

### Requirement: Local overrides via settings.local.toml
The system SHALL support `settings.local.toml` for local developer overrides. This file SHALL be gitignored. Values in `settings.local.toml` SHALL take precedence over `settings.toml` but not over explicit env vars.

#### Scenario: Developer overrides a setting locally
- **WHEN** `settings.local.toml` contains `[default.engine]\npoll_interval = 2`
- **THEN** `config.engine_poll_interval` is `2` regardless of the value in `settings.toml`

#### Scenario: Env var overrides local override
- **WHEN** `settings.local.toml` sets `poll_interval = 2` and env var `ENGINE_POLL_INTERVAL=30` is set
- **THEN** `config.engine_poll_interval` is `30` (env var wins)

### Requirement: Environment layering
The system SHALL support dynaconf environments (`default`, `development`, `production`). The active environment SHALL be set via `ENV_FOR_DYNACONF` env var (default: `default`). Environment-specific sections in `settings.toml` SHALL override values from the `[default]` section.

#### Scenario: Production environment with different log level
- **WHEN** `settings.toml` contains `[default]\nlog_level = "DEBUG"` and `[production]\nlog_level = "WARNING"` and `ENV_FOR_DYNACONF=production`
- **THEN** `config.log_level` is `"WARNING"`

#### Scenario: No environment set
- **WHEN** `ENV_FOR_DYNACONF` is not set
- **THEN** the `[default]` section values are used

### Requirement: Secrets remain in env vars
The system SHALL continue to load all secret values (API keys, tokens, passwords, connection strings) from environment variables via `os.getenv()`. The following fields SHALL NOT be read from `settings.toml`: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GITLAB_TOKEN`, `GH_TOKEN`, `TRELLO_API_KEY`, `TRELLO_TOKEN`, `POSTGRES_URL`, `POSTGRES_ADMIN_URL`, `PG_APP_PASSWORD`, `DB_PASSWORD`, `REDIS_PASSWORD`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`.

#### Scenario: Secret in settings.toml is ignored
- **WHEN** `settings.toml` contains `gitlab_token = "glpat-fake"`
- **THEN** `config.gitlab_token` is loaded from `os.getenv("GITLAB_TOKEN")`, not from the TOML file

#### Scenario: Secret loaded from env var
- **WHEN** env var `GITLAB_TOKEN=glpat-real` is set
- **THEN** `config.gitlab_token` is `"glpat-real"`

### Requirement: Env var backward compatibility for settings
The system SHALL accept legacy env var names for all non-secret settings as overrides. When both a TOML value and a legacy env var are set, the env var SHALL win. This ensures existing `.env` files with settings mixed in continue to work without changes.

#### Scenario: Legacy env var overrides TOML setting
- **WHEN** `settings.toml` has `[default.engine]\nagent_timeout = 600` and env var `AGENT_TIMEOUT=300` is set
- **THEN** `config.agent_timeout` is `300`

#### Scenario: No env var set, TOML value used
- **WHEN** `settings.toml` has `[default.engine]\nagent_timeout = 600` and no `AGENT_TIMEOUT` env var exists
- **THEN** `config.agent_timeout` is `600`

### Requirement: Config.from_env() preserved as alias
The system SHALL keep `Config.from_env()` as a working method that delegates to `Config.load()`. All existing call sites SHALL continue to work without modification.

#### Scenario: Existing code calls Config.from_env()
- **WHEN** code calls `Config.from_env()`
- **THEN** it returns a fully populated `Config` instance identical to `Config.load()`

### Requirement: settings.toml checked into repository
A `settings.toml` file SHALL be created and checked into the repository with sensible defaults for all non-secret settings. It SHALL serve as documentation of all available settings and their default values.

#### Scenario: Fresh clone has working defaults
- **WHEN** a developer clones the repo and runs the app without creating any config files
- **THEN** the app starts with all defaults from `settings.toml` (only secrets need to be provided via env vars)

### Requirement: .env.example contains only secrets
The `.env.example` file SHALL be updated to contain only secret/credential env vars. Non-secret settings SHALL be removed and a comment SHALL reference `settings.toml` for configuration.

#### Scenario: Developer reads .env.example
- **WHEN** a developer opens `.env.example`
- **THEN** they see only API keys, tokens, passwords, and connection strings — no polling intervals, feature flags, or concurrency limits
