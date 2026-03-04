"""Configuration for the PR review agent."""

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


@dataclass
class Config:
    """Configuration loaded from environment variables."""

    # LiteLLM model (any litellm-supported model string)
    model: str = "gpt-4o"

    # MCP server
    mcp_port: int = 8321
    mcp_host: str = "localhost"

    # Database
    db_backend: str = "sqlite"  # sqlite, postgres
    db_path: str = "reviews.db"
    postgres_url: str = ""
    postgres_pool_min: int = 2
    postgres_pool_max: int = 10

    # Agent settings
    agent_timeout: int = 600
    agent_log_dir: str = "logs/agents"

    # Git provider defaults
    git_provider: str = "gitlab"
    gitlab_url: str = ""
    gitlab_token: str = ""
    github_token: str = ""

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
    max_concurrent_jobs: int = 3
    max_revisions: int = 3
    dry_run: bool = False

    # Trello poller (optional)
    trello_api_key: str = ""
    trello_token: str = ""
    trello_board_id: str = ""
    trello_poll_interval: int = 180

    # AWS
    aws_profile: str = "mcp-minions"

    # S3 artifact upload (optional — set bucket to enable)
    s3_artifact_bucket: str = ""
    s3_artifact_region: str = "us-east-1"
    s3_artifact_prefix: str = "minions"

    # Container / deployment settings
    repo_base_dir: str = "/repos"
    mcp_connect_host: str = "localhost"

    @classmethod
    def from_env(cls) -> Config:
        """Load configuration from environment variables."""
        base = Path(__file__).parent.parent

        return cls(
            model=os.getenv("LITELLM_MODEL", os.getenv("MODEL", "gpt-4o")),
            mcp_port=int(os.getenv("MCP_PORT", "8321")),
            mcp_host=os.getenv("MCP_HOST", "localhost"),
            db_backend=os.getenv("DB_BACKEND", "sqlite"),
            db_path=os.getenv("DB_PATH", str(base / "reviews.db")),
            postgres_url=_build_postgres_url(),
            postgres_pool_min=int(os.getenv("PG_POOL_MIN", "2")),
            postgres_pool_max=int(os.getenv("PG_POOL_MAX", "10")),
            agent_timeout=int(os.getenv("AGENT_TIMEOUT", "600")),
            agent_log_dir=os.getenv("AGENT_LOG_DIR", str(base / "logs" / "agents")),
            git_provider=os.getenv("GIT_PROVIDER", "gitlab"),
            gitlab_url=os.getenv("GITLAB_URL", ""),
            gitlab_token=os.getenv("GITLAB_TOKEN", ""),
            github_token=os.getenv("GH_TOKEN", os.getenv("GITHUB_TOKEN", "")),
            engine_poll_interval=int(os.getenv("ENGINE_POLL_INTERVAL", "10")),
            max_concurrent_reviews=int(os.getenv("MAX_CONCURRENT_REVIEWS", "3")),
            nats_enabled=os.getenv("NATS_ENABLED", "").lower() in ("1", "true", "yes"),
            nats_stream=os.getenv("NATS_STREAM", "minions"),
            projects_file=os.getenv("PROJECTS_FILE", str(base / "projects.yaml")),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            arbiter_enabled=os.getenv("ARBITER_ENABLED", "").lower() in ("1", "true", "yes"),
            agent_dispatch_mode=os.getenv("AGENT_DISPATCH_MODE", "in_process"),
            k8s_dispatch=os.getenv("K8S_DISPATCH", "").lower() in ("1", "true", "yes"),
            k8s_namespace=os.getenv("K8S_NAMESPACE", "minion-suite"),
            k8s_agent_image=os.getenv("K8S_AGENT_IMAGE", ""),
            k8s_agent_sa=os.getenv("K8S_AGENT_SERVICE_ACCOUNT", "minion-suite-agent"),
            k8s_job_ttl=int(os.getenv("K8S_JOB_TTL_SECONDS", "3600")),
            k8s_secrets_name=os.getenv("K8S_SECRETS_NAME", "minion-suite-secrets"),
            job_engine_poll_interval=int(os.getenv("JOB_ENGINE_POLL_INTERVAL", "5")),
            max_concurrent_jobs=int(os.getenv("MAX_CONCURRENT_JOBS", "3")),
            max_revisions=int(os.getenv("MAX_REVISIONS", "3")),
            dry_run=os.getenv("DRY_RUN", "").lower() in ("1", "true", "yes"),
            trello_api_key=os.getenv("TRELLO_API_KEY", ""),
            trello_token=os.getenv("TRELLO_TOKEN", ""),
            trello_board_id=os.getenv("TRELLO_BOARD_ID", ""),
            trello_poll_interval=int(os.getenv("TRELLO_POLL_INTERVAL", "180")),
            aws_profile=os.getenv("AWS_PROFILE_NAME", "mcp-minions"),
            s3_artifact_bucket=os.getenv("S3_ARTIFACT_BUCKET", ""),
            s3_artifact_region=os.getenv("S3_ARTIFACT_REGION", "us-east-1"),
            s3_artifact_prefix=os.getenv("S3_ARTIFACT_PREFIX", "minions"),
            repo_base_dir=os.getenv("REPO_BASE_DIR", "/repos"),
            mcp_connect_host=os.getenv("MCP_CONNECT_HOST", "localhost"),
        )

    @property
    def mcp_url(self) -> str:
        return f"http://{self.mcp_host}:{self.mcp_port}/sse"
