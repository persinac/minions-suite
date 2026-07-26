"""Configuration for the PR review agent.

Settings (intervals, flags, limits) are loaded from settings.toml via dynaconf.
Secrets (API keys, tokens, passwords) are loaded from environment variables.
Legacy env vars override TOML values for backward compatibility.
"""

import os
from dataclasses import dataclass
from pathlib import Path


def _build_postgres_url() -> str:
    """Build Postgres URL from explicit env var or DB_* components."""
    explicit = os.getenv("POSTGRES_URL", os.getenv("DATABASE_URL", ""))
    if explicit:
        return explicit

    host = os.getenv("DB_HOST", "")
    if not host:
        return ""

    user = os.getenv("DB_ADMIN", "")
    password = os.getenv("DB_PASSWORD", "")
    port = os.getenv("DB_PORT", "25061")
    dbname = os.getenv("DB_NAME", "fbf-conn-pool")
    return f"postgresql://{user}:{password}@{host}:{port}/{dbname}?sslmode=require"


def _init_dynaconf():
    """Initialize the dynaconf settings instance.

    Loads settings.toml + settings.local.toml from the project root.
    Returns None if dynaconf is not available or files are missing — the
    app falls back to env vars + dataclass defaults.
    """
    try:
        from dynaconf import Dynaconf

        root = Path(__file__).parent.parent
        settings_path = root / "settings.toml"
        local_path = root / "settings.local.toml"

        settings_files = [str(settings_path)]
        if local_path.exists():
            settings_files.append(str(local_path))

        return Dynaconf(
            settings_files=settings_files,
            environments=True,
            env_switcher="ENV_FOR_DYNACONF",
            envvar_prefix="MINION_SETTINGS",  # prevent dynaconf from reading raw env vars
            load_dotenv=False,
        )
    except Exception:
        return None


# Module-level dynaconf instance (loaded once)
_settings = _init_dynaconf()


def _get(section: str, key: str, default=None):
    """Read a value from dynaconf settings by section.key path.

    Returns default if dynaconf is not initialized or the key doesn't exist.
    """
    if _settings is None:
        return default
    try:
        section_data = _settings.get(section)
        if section_data is None:
            return default
        if isinstance(section_data, dict):
            return section_data.get(key, default)
        return default
    except Exception:
        return default


def _get_top(key: str, default=None):
    """Read a top-level dynaconf value."""
    if _settings is None:
        return default
    try:
        val = _settings.get(key)
        if val is None:
            return default
        return val
    except Exception:
        return default


def _get_nested(section: str, subsection: str, key: str, default=None):
    """Read a nested value: section.subsection.key."""
    if _settings is None:
        return default
    try:
        section_data = _settings.get(section)
        if not isinstance(section_data, dict):
            return default
        sub_data = section_data.get(subsection)
        if not isinstance(sub_data, dict):
            return default
        return sub_data.get(key, default)
    except Exception:
        return default


def _env_or(env_var: str, dynaconf_val, default):
    """Env var wins, then dynaconf, then dataclass default."""
    env = os.getenv(env_var)
    if env is not None:
        return env
    if dynaconf_val is not None:
        return dynaconf_val
    return default


def _env_or_int(env_var: str, dynaconf_val, default: int) -> int:
    """Env var (cast to int) wins, then dynaconf, then default."""
    env = os.getenv(env_var)
    if env is not None:
        return int(env)
    if dynaconf_val is not None:
        return int(dynaconf_val)
    return default


def _env_or_float(env_var: str, dynaconf_val, default: float) -> float:
    """Env var (cast to float) wins, then dynaconf, then default."""
    env = os.getenv(env_var)
    if env is not None:
        return float(env)
    if dynaconf_val is not None:
        return float(dynaconf_val)
    return default


def _env_or_bool(env_var: str, dynaconf_val, default: bool) -> bool:
    """Env var (truthy check) wins, then dynaconf, then default."""
    env = os.getenv(env_var)
    if env is not None:
        return env.lower() in ("1", "true", "yes")
    if dynaconf_val is not None:
        if isinstance(dynaconf_val, bool):
            return dynaconf_val
        return str(dynaconf_val).lower() in ("1", "true", "yes")
    return default


@dataclass
class Config:
    """Configuration loaded from settings.toml + environment variables."""

    # LiteLLM model (any litellm-supported model string)
    model: str = "claude-opus-5"

    # MCP server
    mcp_port: int = 8321
    mcp_host: str = "localhost"

    # Database
    postgres_url: str = ""
    postgres_pool_min: int = 2
    postgres_pool_max: int = 10

    # Agent settings
    agent_timeout: int = 600
    agent_log_dir: str = "logs/agents"

    # Spend ceilings, in USD. 0 disables a limit.
    #
    # Nothing bounded cost before this: the loop stopped on turns or wall-clock
    # only, and max_turns was hardcoded at 100. A single backend_engineer run
    # billed $20.57 over 64 turns and had headroom for ~50% more.
    #
    # agent_cost_limit_usd is enforced inside the tool-use loop and reuses the
    # existing wind-down escalation, so an agent nearing its budget is told to
    # commit and ship rather than being killed with its work on the floor.
    # job_cost_limit_usd is checked before each new agent launches, which is
    # what stops a job from spending without bound across many agents.
    agent_cost_limit_usd: float = 8.0
    job_cost_limit_usd: float = 25.0
    agent_max_turns: int = 60

    # Block auto-merge unless every branch-protection required check is green
    # on the PR head. FAIL-CLOSED: a repo with no required checks blocks agent
    # merges, which is deliberate pressure to gate the least-verified repos.
    require_ci_pass: bool = True

    # Job admission rate caps. The cost ceilings bound what one job can spend;
    # these bound how many jobs can spend at all, which is the difference
    # between one bad job and a bad afternoon. Deliberately set high — they are
    # a backstop against a runaway intake loop, not a workload quota.
    # An over-cap job is deferred (left at spec_received), not failed, so it
    # starts on its own once the window clears.
    max_jobs_per_hour: int = 20
    max_jobs_per_month: int = 500

    # Difficulty-based model tiers. One cheap classifier call per job picks the
    # tier, and every agent on that job uses it. Easy vs hard is a 5x difference
    # on both input and output pricing, so a single correct "easy" verdict pays
    # for several hundred classifications.
    classifier_enabled: bool = True
    classifier_model: str = "claude-haiku-4-5"
    classifier_max_chars: int = 6000
    model_easy: str = "claude-haiku-4-5"
    model_medium: str = "claude-sonnet-5"
    model_hard: str = "claude-opus-5"

    # Reviewers run per-specialty and fan out, so their cost multiplies where the
    # engineer's does not: ~4 specialists on one PR against a job ceiling that has
    # already absorbed an engineer. Sonnet holds up well on review; the difficulty
    # classifier still pulls this down to the easy tier on trivial tickets.
    model_reviewer: str = "claude-sonnet-5"

    # Engineer override. Empty = follow the difficulty tier, so behaviour is
    # unchanged until set deliberately. This is the lever that matters: the
    # engineer was 79% of the one measured job's cost, and its workload is
    # input-dominated (3.85M in vs 53k out), so a cheaper input rate compounds.
    # Provider prefix is load-bearing — 'moonshot/kimi-k2.6' is priced by
    # LiteLLM, 'openai/kimi-k2.6' is not, and an unpriced model makes the spend
    # ceilings silently inert. assert_priceable refuses that.
    model_engineer: str = ""

    # Git provider defaults
    git_provider: str = "gitlab"
    gitlab_url: str = ""
    gitlab_token: str = ""
    github_token: str = ""
    # GitHub App — preferred over a static PAT: tokens are minted per hour,
    # scoped to the repos the App is installed on, and revoked by uninstalling.
    # When all three are set they take precedence over github_token.
    github_app_id: str = ""
    github_app_private_key: str = ""  # SECRET (PEM)
    github_app_installation_id: str = ""

    # Second App used only to post reviews. GitHub refuses a formal review from
    # the identity that opened the PR, and the App above opens every minion PR,
    # so a distinct identity is the only way to get a real APPROVED /
    # CHANGES_REQUESTED. Unset = reviews degrade to a PR comment.
    github_reviewer_app_id: str = ""
    github_reviewer_app_private_key: str = ""  # SECRET (PEM)
    github_reviewer_app_installation_id: str = ""

    # Review engine
    engine_poll_interval: int = 10
    max_concurrent_reviews: int = 3

    # NATS (optional)
    nats_enabled: bool = False
    nats_stream: str = "minions"

    # Projects config
    projects_file: str = "projects.yaml"

    # Logging
    log_level: str = "INFO"

    # Arbiter coordination service (requires nats_enabled=True)
    arbiter_enabled: bool = False

    # Agent dispatch mode: "in_process" (LiteLLM loop) or "k8s" (Kubernetes Jobs)
    agent_dispatch_mode: str = "in_process"

    # K8s dispatch settings
    k8s_dispatch: bool = False
    k8s_namespace: str = "minion-suite"
    k8s_agent_image: str = ""
    k8s_agent_sa: str = "minion-suite-agent"
    k8s_job_ttl: int = 3600
    k8s_secrets_name: str = "minion-suite-secrets"

    # Job engine
    job_engine_poll_interval: int = 5
    max_concurrent_jobs: int = 1
    # Whether this process runs a JobEngine. Both --server and --pollers construct
    # one, and job advancement has no cross-process lock: two engines polling the
    # same database will both advance a job and each dispatch its own agents (see
    # launch_spec_analyst, whose only guard is a _has_running_agent read). Exactly
    # one engine should run per deployment. Pollers don't need it — they only write
    # jobs to the DB — so set ENGINE_ENABLED=false there.
    engine_enabled: bool = True
    max_revisions: int = 3
    dry_run: bool = False

    # GitLab issues poller (optional)
    gitlab_issues_enabled: bool = False
    gitlab_issues_poll_interval: int = 120

    # Trello poller (optional)
    trello_api_key: str = ""
    trello_token: str = ""
    trello_board_id: str = ""
    trello_poll_interval: int = 180
    # Only pick up cards carrying the `minion` label. The on-deck column is
    # the team's shared backlog, not a minions queue - without this the poller
    # treats every ticket in it as work, including ones it has no toolchain
    # for and can only fail at. Opt-in by default.
    trello_require_label: bool = True
    # Minimum gap between two jobs admitted FROM THE QUEUE, in seconds. 0 disables.
    #
    # Deliberately separate from trello_poll_interval: the same loop also runs
    # _monitor_jobs(), which moves cards for finished work, so slowing the poll
    # to throttle spend would also leave completed cards sitting in "In progress"
    # for hours. This throttles intake only — monitoring stays responsive.
    #
    # Measured against job CREATION time in the database, not an in-process
    # timer, so a pod restart cannot reset the clock and let a fresh job through
    # early. Does not gate MCP submit_spec: a human asking for work directly is
    # not the runaway case this guards against.
    trello_min_job_interval: int = 0

    # Renovate auto-merge (optional)
    renovate_enabled: bool = False
    renovate_poll_interval: int = 60
    renovate_max_concurrent: int = 2

    # AWS
    aws_profile: str = "mcp-minions"

    # S3 artifact upload (optional — set bucket to enable)
    s3_artifact_bucket: str = ""
    s3_artifact_region: str = "us-east-1"
    s3_artifact_prefix: str = "minions"

    # Memory system (agent-memory)
    memory_enabled: bool = False
    redis_url: str = "redis://localhost:6379"
    redis_password: str = ""
    memory_l3_token_budget: int = 2000
    memory_log_level: str = "INFO"

    # LangGraph feature flags
    use_langgraph_agent: bool = True
    use_langgraph_engine: bool = True

    # Langfuse (optional, LLM observability)
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"

    # Container / deployment settings
    repo_base_dir: str = "/repos"
    mcp_connect_host: str = "localhost"

    @classmethod
    def load(cls) -> Config:
        """Load configuration from settings.toml (via dynaconf) + env var secrets.

        Precedence (highest wins):
          1. Explicit env var (legacy name, e.g. AGENT_TIMEOUT=300)
          2. settings.local.toml
          3. settings.toml
          4. Dataclass defaults
        """
        base = Path(__file__).parent.parent

        return cls(
            # -- Top-level settings (dynaconf top-level keys) --
            model=_env_or("LITELLM_MODEL", os.getenv("MODEL") or _get_top("model"), "claude-opus-5"),
            log_level=_env_or("LOG_LEVEL", _get_top("log_level"), "INFO"),
            projects_file=_env_or("PROJECTS_FILE", _get_top("projects_file"), str(base / "projects.yaml")),
            dry_run=_env_or_bool("DRY_RUN", _get_top("dry_run"), False),
            # -- Server --
            mcp_port=_env_or_int("MCP_PORT", _get("server", "mcp_port"), 8321),
            mcp_host=_env_or("MCP_HOST", _get("server", "mcp_host"), "localhost"),
            mcp_connect_host=_env_or("MCP_CONNECT_HOST", _get("server", "mcp_connect_host"), "localhost"),
            repo_base_dir=_env_or("REPO_BASE_DIR", _get("server", "repo_base_dir"), "/repos"),
            # -- Engine --
            engine_poll_interval=_env_or_int("ENGINE_POLL_INTERVAL", _get("engine", "poll_interval"), 10),
            job_engine_poll_interval=_env_or_int("JOB_ENGINE_POLL_INTERVAL", _get("engine", "job_poll_interval"), 5),
            max_concurrent_reviews=_env_or_int("MAX_CONCURRENT_REVIEWS", _get("engine", "max_concurrent_reviews"), 3),
            max_concurrent_jobs=_env_or_int("MAX_CONCURRENT_JOBS", _get("engine", "max_concurrent_jobs"), 1),
            engine_enabled=_env_or_bool("ENGINE_ENABLED", _get("engine", "enabled"), True),
            max_revisions=_env_or_int("MAX_REVISIONS", _get("engine", "max_revisions"), 3),
            agent_timeout=_env_or_int("AGENT_TIMEOUT", _get("engine", "agent_timeout"), 600),
            agent_cost_limit_usd=_env_or_float("AGENT_COST_LIMIT_USD", _get("engine", "agent_cost_limit_usd"), 8.0),
            job_cost_limit_usd=_env_or_float("JOB_COST_LIMIT_USD", _get("engine", "job_cost_limit_usd"), 25.0),
            agent_max_turns=_env_or_int("AGENT_MAX_TURNS", _get("engine", "agent_max_turns"), 60),
            require_ci_pass=_env_or_bool("REQUIRE_CI_PASS", _get("engine", "require_ci_pass"), True),
            max_jobs_per_hour=_env_or_int("MAX_JOBS_PER_HOUR", _get("engine", "max_jobs_per_hour"), 20),
            max_jobs_per_month=_env_or_int("MAX_JOBS_PER_MONTH", _get("engine", "max_jobs_per_month"), 500),
            classifier_enabled=_env_or_bool("CLASSIFIER_ENABLED", _get("engine", "classifier_enabled"), True),
            classifier_model=_env_or("CLASSIFIER_MODEL", _get("engine", "classifier_model"), "claude-haiku-4-5"),
            classifier_max_chars=_env_or_int("CLASSIFIER_MAX_CHARS", _get("engine", "classifier_max_chars"), 6000),
            model_easy=_env_or("MODEL_EASY", _get("engine", "model_easy"), "claude-haiku-4-5"),
            model_medium=_env_or("MODEL_MEDIUM", _get("engine", "model_medium"), "claude-sonnet-5"),
            model_hard=_env_or("MODEL_HARD", _get("engine", "model_hard"), "claude-opus-5"),
            model_reviewer=_env_or("MODEL_REVIEWER", _get("engine", "model_reviewer"), "claude-sonnet-5"),
            model_engineer=_env_or("MODEL_ENGINEER", _get("engine", "model_engineer"), ""),
            agent_log_dir=_env_or("AGENT_LOG_DIR", _get("engine", "agent_log_dir"), str(base / "logs" / "agents")),
            agent_dispatch_mode=_env_or("AGENT_DISPATCH_MODE", _get("engine", "agent_dispatch_mode"), "in_process"),
            # -- Database --
            postgres_url=_build_postgres_url(),  # secret — always from env
            postgres_pool_min=_env_or_int("PG_POOL_MIN", _get("database", "pool_min"), 2),
            postgres_pool_max=_env_or_int("PG_POOL_MAX", _get("database", "pool_max"), 10),
            # -- Git (secrets from env, settings from TOML) --
            git_provider=_env_or("GIT_PROVIDER", _get("git", "provider"), "gitlab"),
            gitlab_url=_env_or("GITLAB_URL", _get("git", "gitlab_url"), ""),
            gitlab_token=os.getenv("GITLAB_TOKEN", ""),  # SECRET
            github_token=os.getenv("GH_TOKEN", os.getenv("GITHUB_TOKEN", "")),  # SECRET
            github_app_id=os.getenv("GITHUB_APP_ID", ""),
            github_app_private_key=os.getenv("GITHUB_APP_PRIVATE_KEY", ""),  # SECRET
            github_app_installation_id=os.getenv("GITHUB_APP_INSTALLATION_ID", ""),
            # GITHUB_APP_REVIEWER_* keeps the GITHUB_APP_ prefix shared with the
            # engineer App above, so the two read as a pair. This matches the
            # names already set in Doppler mcp-minions/prd.
            github_reviewer_app_id=os.getenv("GITHUB_APP_REVIEWER_ID", ""),
            github_reviewer_app_private_key=os.getenv("GITHUB_APP_REVIEWER_PRIVATE_KEY", ""),  # SECRET
            github_reviewer_app_installation_id=os.getenv("GITHUB_APP_REVIEWER_INSTALLATION_ID", ""),
            # -- NATS --
            nats_enabled=_env_or_bool("NATS_ENABLED", _get("nats", "enabled"), False),
            nats_stream=_env_or("NATS_STREAM", _get("nats", "stream"), "minions"),
            # -- Arbiter --
            arbiter_enabled=_env_or_bool("ARBITER_ENABLED", _get("arbiter", "enabled"), False),
            # -- K8s --
            k8s_dispatch=_env_or_bool("K8S_DISPATCH", _get("k8s", "dispatch"), False),
            k8s_namespace=_env_or("K8S_NAMESPACE", _get("k8s", "namespace"), "minion-suite"),
            k8s_agent_image=_env_or("K8S_AGENT_IMAGE", _get("k8s", "agent_image"), ""),
            k8s_agent_sa=_env_or("K8S_AGENT_SERVICE_ACCOUNT", _get("k8s", "agent_sa"), "minion-suite-agent"),
            k8s_job_ttl=_env_or_int("K8S_JOB_TTL_SECONDS", _get("k8s", "job_ttl"), 3600),
            k8s_secrets_name=_env_or("K8S_SECRETS_NAME", _get("k8s", "secrets_name"), "minion-suite-secrets"),
            # -- Pollers: GitLab Issues --
            gitlab_issues_enabled=_env_or_bool("GITLAB_ISSUES_ENABLED", _get_nested("pollers", "gitlab_issues", "enabled"), False),
            gitlab_issues_poll_interval=_env_or_int("GITLAB_ISSUES_POLL_INTERVAL", _get_nested("pollers", "gitlab_issues", "poll_interval"), 120),
            # -- Pollers: Trello (keys are secrets, intervals are settings) --
            trello_api_key=os.getenv("TRELLO_API_KEY", ""),  # SECRET
            trello_token=os.getenv("TRELLO_TOKEN", ""),  # SECRET
            trello_board_id=os.getenv("TRELLO_BOARD_ID", ""),  # credential-adjacent
            trello_poll_interval=_env_or_int("TRELLO_POLL_INTERVAL", _get_nested("pollers", "trello", "poll_interval"), 180),
            trello_require_label=_env_or_bool("TRELLO_REQUIRE_LABEL", _get_nested("pollers", "trello", "require_label"), True),
            trello_min_job_interval=_env_or_int("TRELLO_MIN_JOB_INTERVAL", _get_nested("pollers", "trello", "min_job_interval"), 0),
            # -- Pollers: Renovate --
            renovate_enabled=_env_or_bool("RENOVATE_ENABLED", _get_nested("pollers", "renovate", "enabled"), False),
            renovate_poll_interval=_env_or_int("RENOVATE_POLL_INTERVAL", _get_nested("pollers", "renovate", "poll_interval"), 60),
            renovate_max_concurrent=_env_or_int("RENOVATE_MAX_CONCURRENT", _get_nested("pollers", "renovate", "max_concurrent"), 2),
            # -- AWS --
            aws_profile=_env_or("AWS_PROFILE_NAME", _get("aws", "profile"), "mcp-minions"),
            # -- S3 Artifacts --
            s3_artifact_bucket=_env_or("S3_ARTIFACT_BUCKET", _get("artifacts", "bucket"), ""),
            s3_artifact_region=_env_or("S3_ARTIFACT_REGION", _get("artifacts", "region"), "us-east-1"),
            s3_artifact_prefix=_env_or("S3_ARTIFACT_PREFIX", _get("artifacts", "prefix"), "minions"),
            # -- Memory --
            memory_enabled=_env_or_bool("MEMORY_ENABLED", _get("memory", "enabled"), False),
            redis_url=os.getenv("REDIS_URL", "redis://localhost:6379"),  # connection string — treat as secret
            redis_password=os.getenv("REDIS_PASSWORD", ""),  # SECRET
            memory_l3_token_budget=_env_or_int("MEMORY_L3_TOKEN_BUDGET", _get("memory", "l3_token_budget"), 2000),
            memory_log_level=_env_or("MEMORY_LOG_LEVEL", _get("memory", "log_level"), "INFO"),
            # -- LangGraph --
            use_langgraph_agent=_env_or_bool("USE_LANGGRAPH_AGENT", _get("langgraph", "use_agent"), True),
            use_langgraph_engine=_env_or_bool("USE_LANGGRAPH_ENGINE", _get("langgraph", "use_engine"), True),
            # -- Langfuse (keys are secrets, host is a setting) --
            langfuse_public_key=os.getenv("LANGFUSE_PUBLIC_KEY", ""),  # SECRET
            langfuse_secret_key=os.getenv("LANGFUSE_SECRET_KEY", ""),  # SECRET
            langfuse_host=_env_or("LANGFUSE_OTEL_HOST", _get("langfuse", "host"), "https://cloud.langfuse.com"),
        )

    @classmethod
    def from_env(cls) -> Config:
        """Load configuration. Alias for Config.load() — backward compatible."""
        return cls.load()

    @property
    def mcp_url(self) -> str:
        return f"http://{self.mcp_host}:{self.mcp_port}/sse"
