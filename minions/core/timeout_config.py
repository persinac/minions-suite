"""Timeout and circuit breaker configuration for the Arbiter service."""

from dataclasses import dataclass, field


@dataclass
class RoleTimeoutConfig:
    """Per-role timeout settings for subtask and task execution."""

    subtask_timeout_seconds: int = 120
    task_timeout_seconds: int = 600
    max_subtask_retries: int = 2


def _default_role_configs() -> dict[str, RoleTimeoutConfig]:
    return {
        "spec_analyst": RoleTimeoutConfig(subtask_timeout_seconds=180, task_timeout_seconds=1200),
        "arbiter": RoleTimeoutConfig(subtask_timeout_seconds=180, task_timeout_seconds=1200),
        "backend_engineer": RoleTimeoutConfig(subtask_timeout_seconds=600, task_timeout_seconds=1200),
        "frontend_engineer": RoleTimeoutConfig(subtask_timeout_seconds=600, task_timeout_seconds=1200),
        "database_engineer": RoleTimeoutConfig(subtask_timeout_seconds=600, task_timeout_seconds=600),
        "code_reviewer": RoleTimeoutConfig(subtask_timeout_seconds=180, task_timeout_seconds=600),
        "deploy_monitor": RoleTimeoutConfig(subtask_timeout_seconds=180, task_timeout_seconds=600),
    }


@dataclass
class TimeoutConfig:
    """Configuration for heartbeat monitoring, timeouts, and circuit breaker."""

    heartbeat_interval_seconds: int = 30
    heartbeat_stale_threshold_seconds: int = 300
    monitor_loop_interval_seconds: int = 30
    roles: dict[str, RoleTimeoutConfig] = field(default_factory=_default_role_configs)
    circuit_breaker_failure_threshold: int = 3
    circuit_breaker_window_seconds: int = 120
    circuit_breaker_cooldown_seconds: int = 300
    stuck_task_threshold_minutes: int = 15
    anomaly_check_enabled: bool = True
