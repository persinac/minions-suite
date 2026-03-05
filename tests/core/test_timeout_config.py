"""Tests for timeout and circuit breaker configuration."""

from minions.core.timeout_config import RoleTimeoutConfig, TimeoutConfig


class TestRoleTimeoutConfig:
    def test_defaults(self):
        cfg = RoleTimeoutConfig()
        assert cfg.subtask_timeout_seconds == 120
        assert cfg.task_timeout_seconds == 600
        assert cfg.max_subtask_retries == 2

    def test_custom_values(self):
        cfg = RoleTimeoutConfig(subtask_timeout_seconds=300, task_timeout_seconds=1200, max_subtask_retries=5)
        assert cfg.subtask_timeout_seconds == 300
        assert cfg.task_timeout_seconds == 1200
        assert cfg.max_subtask_retries == 5


class TestTimeoutConfig:
    def test_defaults(self):
        cfg = TimeoutConfig()
        assert cfg.heartbeat_interval_seconds == 30
        assert cfg.heartbeat_stale_threshold_seconds == 300
        assert cfg.circuit_breaker_failure_threshold == 3
        assert cfg.anomaly_check_enabled is True

    def test_default_roles_populated(self):
        cfg = TimeoutConfig()
        assert "spec_analyst" in cfg.roles
        assert "backend_engineer" in cfg.roles
        assert "code_reviewer" in cfg.roles
        assert "deploy_monitor" in cfg.roles
        assert len(cfg.roles) == 7

    def test_engineer_has_longer_subtask_timeout(self):
        cfg = TimeoutConfig()
        eng = cfg.roles["backend_engineer"]
        reviewer = cfg.roles["code_reviewer"]
        assert eng.subtask_timeout_seconds > reviewer.subtask_timeout_seconds

    def test_spec_analyst_timeout(self):
        cfg = TimeoutConfig()
        sa = cfg.roles["spec_analyst"]
        assert sa.task_timeout_seconds == 1200
        assert sa.subtask_timeout_seconds == 180
