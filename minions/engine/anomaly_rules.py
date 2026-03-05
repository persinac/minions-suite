"""Anomaly detection rules for the arbiter monitor loop.

Each rule is an async function that inspects DB state and returns a list of
Anomaly instances describing detected problems and suggested remediation actions.
"""

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from ..db import AbstractDatabase

logger = logging.getLogger(__name__)


@dataclass
class Anomaly:
    rule_name: str
    severity: str  # "warning", "recoverable", "critical"
    entity_type: str  # "task", "job", "subtask"
    entity_id: str
    job_id: Optional[str]
    description: str
    suggested_action: str  # "retry_agent", "fail_job", "advance_job", "alert_only"


async def check_stuck_tasks(db: AbstractDatabase, stuck_threshold_minutes: int = 15) -> list[Anomaly]:
    """Detect tasks stuck at IN_PROGRESS with no subtask activity for too long."""
    anomalies: list[Anomaly] = []
    active_jobs = await db.get_active_jobs()
    now = time.time()

    for job in active_jobs:
        tasks = await db.get_tasks(job.id)
        for task in tasks:
            if task.status not in ("in_progress", "in_review"):
                continue

            try:
                updated = datetime.fromisoformat(task.updated_at)
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=timezone.utc)
                elapsed_minutes = (now - updated.timestamp()) / 60.0
            except (ValueError, TypeError):
                continue

            if elapsed_minutes < stuck_threshold_minutes:
                continue

            subtasks = await db.get_subtasks(task.id)
            has_recent_activity = False
            for st in subtasks:
                if st.status == "running":
                    has_recent_activity = True
                    break
                check_time = st.completed_at if st.completed_at else st.started_at
                if check_time:
                    try:
                        st_time = datetime.fromisoformat(check_time)
                        if st_time.tzinfo is None:
                            st_time = st_time.replace(tzinfo=timezone.utc)
                        if (now - st_time.timestamp()) / 60.0 < stuck_threshold_minutes:
                            has_recent_activity = True
                            break
                    except (ValueError, TypeError):
                        continue

            if has_recent_activity:
                continue

            has_heartbeat = await db.has_recent_heartbeat(task.id, stuck_threshold_minutes * 60)
            if has_heartbeat:
                logger.debug("Task %s has no subtask activity but agent heartbeat is recent, skipping", task.id)
                continue

            anomalies.append(
                Anomaly(
                    rule_name="stuck_task",
                    severity="recoverable",
                    entity_type="task",
                    entity_id=task.id,
                    job_id=job.id,
                    description=f"Task {task.id} stuck at {task.status} for {elapsed_minutes:.0f}min with no subtask activity and no heartbeat",
                    suggested_action="retry_agent",
                )
            )

    return anomalies


async def check_job_task_consistency(db: AbstractDatabase) -> list[Anomaly]:
    """Detect jobs whose status contradicts the actual state of their tasks."""
    anomalies: list[Anomaly] = []
    active_jobs = await db.get_active_jobs()

    for job in active_jobs:
        tasks = await db.get_tasks(job.id)
        if not tasks:
            continue

        dev_roles = {"backend_engineer", "frontend_engineer", "database_engineer"}
        dev_tasks = [t for t in tasks if t.agent_role in dev_roles]
        if not dev_tasks:
            continue

        if job.status == "deploying":
            pre_deploy = {"pending", "in_progress", "pr_open", "in_review"}
            stuck_pre_deploy = [t for t in dev_tasks if t.status in pre_deploy]
            if stuck_pre_deploy:
                anomalies.append(
                    Anomaly(
                        rule_name="job_task_inconsistency",
                        severity="critical",
                        entity_type="job",
                        entity_id=job.id,
                        job_id=job.id,
                        description=f"Job {job.id} is DEPLOYING but {len(stuck_pre_deploy)} tasks are still pre-deploy",
                        suggested_action="fail_job",
                    )
                )

        if job.status == "review_in_progress":
            all_past_review = all(t.status in ("merged", "done", "failed") for t in dev_tasks)
            if all_past_review:
                anomalies.append(
                    Anomaly(
                        rule_name="job_task_inconsistency",
                        severity="recoverable",
                        entity_type="job",
                        entity_id=job.id,
                        job_id=job.id,
                        description=f"Job {job.id} stuck at REVIEW_IN_PROGRESS but all dev tasks are past review",
                        suggested_action="advance_job",
                    )
                )

        if job.status == "dev_in_progress":
            past_dev = {"pr_open", "in_review", "merged", "done", "failed"}
            all_past = all(t.status in past_dev for t in dev_tasks)
            if all_past:
                anomalies.append(
                    Anomaly(
                        rule_name="job_task_inconsistency",
                        severity="recoverable",
                        entity_type="job",
                        entity_id=job.id,
                        job_id=job.id,
                        description=f"Job {job.id} stuck at DEV_IN_PROGRESS but all dev tasks are past dev",
                        suggested_action="advance_job",
                    )
                )

    return anomalies


async def check_missing_required_fields(db: AbstractDatabase) -> list[Anomaly]:
    """Detect tasks that reached certain statuses without required fields populated."""
    anomalies: list[Anomaly] = []
    active_jobs = await db.get_active_jobs()

    checks = {
        "pr_open": ["pr_url"],
        "merged": ["pr_url"],
        "in_review": ["pr_url"],
    }

    for job in active_jobs:
        tasks = await db.get_tasks(job.id)
        for task in tasks:
            required_fields = checks.get(task.status)
            if not required_fields:
                continue

            task_data = task.model_dump()
            missing = [f for f in required_fields if not task_data.get(f)]
            if missing:
                anomalies.append(
                    Anomaly(
                        rule_name="missing_required_fields",
                        severity="warning",
                        entity_type="task",
                        entity_id=task.id,
                        job_id=job.id,
                        description=f"Task {task.id} at {task.status} is missing: {', '.join(missing)}",
                        suggested_action="alert_only",
                    )
                )

    return anomalies


ALL_RULES = [
    check_stuck_tasks,
    check_job_task_consistency,
    check_missing_required_fields,
]
