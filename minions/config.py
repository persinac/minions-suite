"""Configuration for the PR review agent."""

import os
from dataclasses import dataclass
from pathlib import Path


def _build_postgres_url() -> str:
    """Build Postgres URL from explicit env var or component parts."""
    explicit = os.getenv("POSTGRES_URL", os.getenv("DATABASE_URL", ""))
    if explicit:
        return explicit

    host = os.getenv("PG_HOST", "")
    if not host:
        return ""

    user = os.getenv("PG_USER", "")
    password = os.getenv("PG_PASSWORD", "")
    port = os.getenv("PG_PORT", "5432")
    dbname = os.getenv("PG_DATABASE", "pr_reviewer")
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
    nats_stream: str = "reviews"

    # Projects config
    projects_file: str = "projects.yaml"

    # Logging
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Config":
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
            nats_stream=os.getenv("NATS_STREAM", "reviews"),
            projects_file=os.getenv("PROJECTS_FILE", str(base / "projects.yaml")),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
        )

    @property
    def mcp_url(self) -> str:
        return f"http://{self.mcp_host}:{self.mcp_port}/sse"
