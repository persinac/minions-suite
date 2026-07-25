"""Tests for dynaconf-based configuration loading."""

import textwrap

import pytest


@pytest.fixture
def settings_dir(tmp_path, monkeypatch):
    """Create a temp directory with settings.toml and point config.py at it."""
    import minions.config as cfg_mod

    # Write a minimal settings.toml
    toml = tmp_path / "settings.toml"
    toml.write_text(
        textwrap.dedent("""\
        [default]
        model = "test-model"
        log_level = "WARNING"
        dry_run = true

        [default.server]
        mcp_port = 9999
        mcp_host = "0.0.0.0"

        [default.engine]
        agent_timeout = 999
        poll_interval = 77
        max_concurrent_reviews = 7

        [default.nats]
        enabled = true

        [default.pollers.gitlab_issues]
        enabled = true
        poll_interval = 42

        [default.pollers.trello]
        poll_interval = 300

        [default.memory]
        enabled = true
        l3_token_budget = 5000

        [default.langfuse]
        host = "http://langfuse.local:3000"

        [default.langgraph]
        use_agent = false
        use_engine = false

        [production]
        log_level = "ERROR"
        """),
        encoding="utf-8",
    )

    # Re-initialize dynaconf to point at the temp settings
    from dynaconf import Dynaconf

    test_settings = Dynaconf(
        settings_files=[str(toml)],
        environments=True,
        env_switcher="ENV_FOR_DYNACONF",
        envvar_prefix="MINION_SETTINGS",
        load_dotenv=False,
    )
    monkeypatch.setattr(cfg_mod, "_settings", test_settings)

    # Clear any env vars that would interfere
    for key in [
        "LITELLM_MODEL",
        "MODEL",
        "LOG_LEVEL",
        "DRY_RUN",
        "MCP_PORT",
        "MCP_HOST",
        "AGENT_TIMEOUT",
        "ENGINE_POLL_INTERVAL",
        "MAX_CONCURRENT_REVIEWS",
        "DB_BACKEND",
        "NATS_ENABLED",
        "GITLAB_ISSUES_ENABLED",
        "GITLAB_ISSUES_POLL_INTERVAL",
        "TRELLO_POLL_INTERVAL",
        "MEMORY_ENABLED",
        "MEMORY_L3_TOKEN_BUDGET",
        "LANGFUSE_OTEL_HOST",
        "USE_LANGGRAPH_AGENT",
        "USE_LANGGRAPH_ENGINE",
        "ENV_FOR_DYNACONF",
    ]:
        monkeypatch.delenv(key, raising=False)

    return tmp_path


def test_load_reads_from_toml(settings_dir):
    from minions.config import Config

    config = Config.load()

    assert config.model == "test-model"
    assert config.log_level == "WARNING"
    assert config.dry_run is True
    assert config.mcp_port == 9999
    assert config.mcp_host == "0.0.0.0"
    assert config.agent_timeout == 999
    assert config.engine_poll_interval == 77
    assert config.max_concurrent_reviews == 7
    assert config.nats_enabled is True
    assert config.gitlab_issues_enabled is True
    assert config.gitlab_issues_poll_interval == 42
    assert config.trello_poll_interval == 300
    assert config.memory_enabled is True
    assert config.memory_l3_token_budget == 5000
    assert config.langfuse_host == "http://langfuse.local:3000"
    assert config.use_langgraph_agent is False
    assert config.use_langgraph_engine is False


def test_env_var_overrides_toml(settings_dir, monkeypatch):
    from minions.config import Config

    monkeypatch.setenv("AGENT_TIMEOUT", "99")
    monkeypatch.setenv("MCP_PORT", "1234")
    monkeypatch.setenv("NATS_ENABLED", "false")

    config = Config.load()

    assert config.agent_timeout == 99
    assert config.mcp_port == 1234
    assert config.nats_enabled is False
    # Non-overridden values still come from TOML
    assert config.model == "test-model"


def test_local_toml_overrides_settings(settings_dir, monkeypatch):
    import minions.config as cfg_mod

    # Write settings.local.toml
    local_toml = settings_dir / "settings.local.toml"
    local_toml.write_text(
        textwrap.dedent("""\
        [default]
        model = "local-override-model"

        [default.engine]
        agent_timeout = 111
        """),
        encoding="utf-8",
    )

    from dynaconf import Dynaconf

    test_settings = Dynaconf(
        settings_files=[str(settings_dir / "settings.toml"), str(local_toml)],
        environments=True,
        env_switcher="ENV_FOR_DYNACONF",
        envvar_prefix="MINION_SETTINGS",
        load_dotenv=False,
    )
    monkeypatch.setattr(cfg_mod, "_settings", test_settings)

    from minions.config import Config

    config = Config.load()

    assert config.model == "local-override-model"
    assert config.agent_timeout == 111
    # Other values still from settings.toml
    assert config.mcp_port == 9999


def test_secrets_not_read_from_toml(settings_dir, monkeypatch):
    """Even if TOML contains secret-like keys, they should be ignored."""
    import minions.config as cfg_mod

    # Write a TOML with secrets (should be ignored)
    toml = settings_dir / "settings.toml"
    content = toml.read_text(encoding="utf-8")
    content += '\ngitlab_token = "bad-token-from-toml"\n'
    toml.write_text(content, encoding="utf-8")

    from dynaconf import Dynaconf

    test_settings = Dynaconf(
        settings_files=[str(toml)],
        environments=True,
        env_switcher="ENV_FOR_DYNACONF",
        envvar_prefix="MINION_SETTINGS",
        load_dotenv=False,
    )
    monkeypatch.setattr(cfg_mod, "_settings", test_settings)

    # Set the real secret via env
    monkeypatch.setenv("GITLAB_TOKEN", "real-token")

    from minions.config import Config

    config = Config.load()

    assert config.gitlab_token == "real-token"

    # Verify other secrets default to empty when env not set
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    monkeypatch.delenv("TRELLO_API_KEY", raising=False)
    monkeypatch.delenv("REDIS_PASSWORD", raising=False)

    config2 = Config.load()
    assert config2.langfuse_public_key == ""
    assert config2.langfuse_secret_key == ""
    assert config2.trello_api_key == ""
    assert config2.redis_password == ""


def test_from_env_is_alias_for_load(settings_dir):
    from minions.config import Config

    config_load = Config.load()
    config_from_env = Config.from_env()

    assert config_load.model == config_from_env.model
    assert config_load.agent_timeout == config_from_env.agent_timeout
    assert config_load.nats_enabled == config_from_env.nats_enabled


def test_loads_without_settings_toml(monkeypatch):
    """App should start fine even if settings.toml doesn't exist."""
    import minions.config as cfg_mod

    monkeypatch.setattr(cfg_mod, "_settings", None)

    # Clear env vars that would set values
    for key in ["LITELLM_MODEL", "MODEL", "AGENT_TIMEOUT", "MCP_PORT"]:
        monkeypatch.delenv(key, raising=False)

    from minions.config import Config

    config = Config.load()

    # Should fall back to dataclass defaults
    assert config.model == "claude-opus-5"
    assert config.agent_timeout == 600
    assert config.mcp_port == 8321
