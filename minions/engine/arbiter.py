"""Arbiter coordination service: NATS request/reply for state transitions,
heartbeat monitoring, stuck detection, and circuit breaker logic."""

import asyncio
import json
import logging
import time
from collections import deque
from datetime import datetime, timezone
from typing import Optional

from ..connectors.nats_client import NatsClient
from ..core.models import SubtaskStatus, TaskStatus, _now
from ..core.state_transitions import InvalidTransitionError, PreconditionError
from ..core.timeout_config import RoleTimeoutConfig, TimeoutConfig
from ..db import AbstractDatabase
from .anomaly_rules import ALL_RULES, Anomaly

logger = logging.getLogger(__name__)


class Arbiter:
    """Central coordination service for state transitions via NATS request/reply."""

    def __init__(self, db: AbstractDatabase, timeout_config: TimeoutConfig, nats_client: NatsClient):
        self.db = db
        self.timeout_config = timeout_config
        self.nats_client = nats_client
        self._monitor_task: Optional[asyncio.Task] = None
        self._running = False

        # Circuit breaker state (in-memory)
        self._failure_timestamps: deque[float] = deque()
        self._circuit_open_until: float = 0.0

    # =========================================================================
    # Lifecycle
    # =========================================================================

    async def start(self) -> None:
        """Subscribe to NATS subjects and start the monitor loop."""
        await self.nats_client.subscribe("arbiter.state.transition", self._handle_transition)
        await self.nats_client.subscribe("arbiter.heartbeat", self._handle_heartbeat)

        self._running = True
        self._monitor_task = asyncio.create_task(self._monitor_loop(), name="arbiter-monitor")
        logger.info("Arbiter started (subscribed to arbiter.state.transition, arbiter.heartbeat)")

    async def stop(self) -> None:
        """Cancel the monitor loop."""
        self._running = False
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            self._monitor_task = None
        logger.info("Arbiter stopped")

    # =========================================================================
    # NATS Request Handlers
    # =========================================================================

    async def _handle_transition(self, msg) -> None:
        """Handle a state transition proposal from an agent via NATS request/reply."""
        try:
            payload = json.loads(msg.data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            await NatsClient.reply(msg, {"approved": False, "error": f"Invalid payload: {e}"})
            return

        entity_type = payload.get("entity_type", "")
        entity_id = payload.get("entity_id", "")
        to_status = payload.get("to_status", "")
        kwargs = payload.get("kwargs", {})
        job_id = payload.get("job_id")

        # Circuit breaker check
        if self._is_circuit_open():
            await NatsClient.reply(
                msg,
                {
                    "approved": False,
                    "error": "circuit_open",
                    "detail": "Too many recent failures, circuit breaker is open",
                },
            )
            return

        try:
            if entity_type == "job":
                response = await self._apply_job_transition(entity_id, to_status, kwargs)
            elif entity_type == "task":
                response = await self._apply_task_transition(entity_id, to_status, kwargs, job_id)
            elif entity_type == "subtask":
                response = await self._apply_subtask_transition(entity_id, to_status, kwargs)
            else:
                response = {"approved": False, "error": f"Unknown entity_type: {entity_type}"}
        except InvalidTransitionError as e:
            self._record_failure()
            response = {
                "approved": False,
                "error": str(e),
                "from_status": e.from_status,
                "to_status": e.to_status,
            }
        except PreconditionError as e:
            self._record_failure()
            response = {
                "approved": False,
                "error": str(e),
                "missing_fields": e.missing_fields,
            }
        except Exception as e:
            self._record_failure()
            logger.exception("Arbiter transition handler error for %s/%s", entity_type, entity_id)
            response = {"approved": False, "error": f"Internal error: {e}"}

        await NatsClient.reply(msg, response)

    async def _apply_job_transition(self, job_id: str, to_status: str, kwargs: dict) -> dict:
        """Apply a job state transition."""
        # Special handling for spec update
        if "refined_spec" in kwargs:
            await self.db.update_job_spec(job_id, kwargs.pop("refined_spec"))

        await self.db.update_job_status(job_id, to_status, error=kwargs.get("error"))
        return {"approved": True, "entity_type": "job", "entity_id": job_id, "to_status": to_status}

    async def _apply_task_transition(self, task_id: str, to_status: str, kwargs: dict, job_id: Optional[str] = None) -> dict:
        """Apply a task state transition."""
        update_kwargs = {"status": to_status}
        for key in ("pr_number", "pr_url", "review_status", "deploy_status", "error", "agent_role"):
            if key in kwargs:
                update_kwargs[key] = kwargs[key]

        task = await self.db.update_task(task_id, **update_kwargs)
        if not task:
            return {"approved": False, "error": f"Task {task_id} not found"}
        return {"approved": True, "entity_type": "task", "entity_id": task_id, "to_status": to_status}

    async def _apply_subtask_transition(self, subtask_id: str, to_status: str, kwargs: dict) -> dict:
        """Apply a subtask state transition."""
        update_kwargs = {"status": to_status}
        if to_status == SubtaskStatus.RUNNING:
            update_kwargs["started_at"] = _now()
        elif to_status in (SubtaskStatus.COMPLETED, SubtaskStatus.FAILED):
            update_kwargs["completed_at"] = _now()
        if "result" in kwargs:
            update_kwargs["result"] = kwargs["result"]
        if "error" in kwargs:
            update_kwargs["error"] = kwargs["error"]

        subtask = await self.db.update_subtask(subtask_id, **update_kwargs)
        if not subtask:
            return {"approved": False, "error": f"Subtask {subtask_id} not found"}
        return {"approved": True, "entity_type": "subtask", "entity_id": subtask_id, "to_status": to_status}

    async def _handle_heartbeat(self, msg) -> None:
        """Handle a heartbeat message from an agent."""
        try:
            payload = json.loads(msg.data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return

        agent_id = payload.get("agent_id", "")
        agent_role = payload.get("agent_role", "")
        job_id = payload.get("job_id")
        task_id = payload.get("current_task_id")
        subtask_id = payload.get("current_subtask_id")
        status = payload.get("status", "active")

        try:
            await self.db.upsert_heartbeat(agent_id, agent_role, job_id, task_id, subtask_id, status)
        except Exception:
            logger.debug("Failed to upsert heartbeat for agent %s", agent_id, exc_info=True)

    # =========================================================================
    # Monitor Loop
    # =========================================================================

    async def _monitor_loop(self) -> None:
        """Periodic loop to detect stale heartbeats, timed-out subtasks, and anomalies."""
        # Grace period: let agents start up and send their first heartbeats
        await asyncio.sleep(self.timeout_config.heartbeat_stale_threshold_seconds)
        while self._running:
            try:
                await self._check_stale_heartbeats()
                await self._check_timed_out_subtasks()
                if self.timeout_config.anomaly_check_enabled:
                    await self._check_anomalies()
            except Exception:
                logger.exception("Error in arbiter monitor loop")
            await asyncio.sleep(self.timeout_config.monitor_loop_interval_seconds)

    async def _check_stale_heartbeats(self) -> None:
        """Detect agents that haven't sent a heartbeat recently."""
        stale = await self.db.get_stale_heartbeats(self.timeout_config.heartbeat_stale_threshold_seconds)
        for hb in stale:
            agent_id = hb.get("agent_id", "unknown")
            job_id = hb.get("job_id")
            logger.warning("Stale heartbeat detected for agent %s (job=%s)", agent_id, job_id)

            # Mark as lost
            try:
                await self.db.upsert_heartbeat(agent_id, hb.get("agent_role", ""), job_id, status="lost")
            except Exception:
                logger.debug("Failed to mark heartbeat as lost for %s", agent_id, exc_info=True)

            # Record event
            if job_id:
                await self.db.record_event(job_id, "heartbeat_lost", "arbiter", f"agent={agent_id}")

            # Publish kill signal
            try:
                await self.nats_client.publish(f"agents.{agent_id}.control", {"action": "kill", "reason": "stale_heartbeat"})
            except Exception:
                logger.debug("Failed to publish kill signal for agent %s", agent_id, exc_info=True)

    async def _check_timed_out_subtasks(self) -> None:
        """Detect running subtasks that have exceeded their role timeout."""
        running = await self.db.get_running_subtasks_all()
        now = time.time()

        for subtask in running:
            if not subtask.started_at:
                continue

            # Parse started_at ISO timestamp
            try:
                started = datetime.fromisoformat(subtask.started_at)
                if started.tzinfo is None:
                    started = started.replace(tzinfo=timezone.utc)
                elapsed = now - started.timestamp()
            except (ValueError, TypeError):
                continue

            # Look up task to get agent_role for timeout config
            task = await self.db.get_task(subtask.task_id)
            if not task:
                continue

            role_config = self.timeout_config.roles.get(task.agent_role, RoleTimeoutConfig())
            if elapsed < role_config.subtask_timeout_seconds:
                continue

            logger.warning("Subtask %s timed out (%.0fs > %ds)", subtask.id, elapsed, role_config.subtask_timeout_seconds)

            try:
                await self.db.update_subtask(subtask.id, status=SubtaskStatus.FAILED, error="Timed out by arbiter", completed_at=_now())
                logger.info("Subtask %s marked failed due to timeout", subtask.id)
            except InvalidTransitionError:
                logger.debug("Could not fail timed-out subtask %s (already transitioned)", subtask.id)

            if task.job_id:
                await self.db.record_event(task.job_id, "subtask_timeout", "arbiter", f"subtask={subtask.id} elapsed={elapsed:.0f}s")

    # =========================================================================
    # Anomaly Detection
    # =========================================================================

    async def _check_anomalies(self) -> None:
        """Run all anomaly detection rules and dispatch remediation actions."""
        for rule_fn in ALL_RULES:
            try:
                if rule_fn.__name__ == "check_stuck_tasks":
                    anomalies = await rule_fn(self.db, self.timeout_config.stuck_task_threshold_minutes)
                else:
                    anomalies = await rule_fn(self.db)
            except Exception:
                logger.debug("Anomaly rule %s failed", rule_fn.__name__, exc_info=True)
                continue

            for anomaly in anomalies:
                logger.warning("Anomaly detected: %s (action=%s)", anomaly.description, anomaly.suggested_action)
                if anomaly.job_id:
                    await self.db.record_event(
                        anomaly.job_id,
                        f"anomaly_{anomaly.rule_name}",
                        "arbiter",
                        f"severity={anomaly.severity} entity={anomaly.entity_type}/{anomaly.entity_id} action={anomaly.suggested_action}",
                    )
                await self._handle_anomaly(anomaly)

    async def _handle_anomaly(self, anomaly: Anomaly) -> None:
        """Route an anomaly to the appropriate remediation action."""
        try:
            if anomaly.suggested_action == "retry_agent":
                await self._trigger_task_retry(anomaly.entity_id, anomaly.job_id, anomaly.description)
            elif anomaly.suggested_action == "fail_job":
                if anomaly.job_id:
                    await self.db.update_job_status(anomaly.job_id, "failed", error=anomaly.description)
            elif anomaly.suggested_action == "advance_job":
                if anomaly.job_id:
                    await self._try_advance_stuck_job(anomaly.job_id)
            # "alert_only" -- event already recorded, nothing else to do
        except Exception:
            logger.debug("Failed to handle anomaly %s for %s", anomaly.rule_name, anomaly.entity_id, exc_info=True)

    async def _trigger_task_retry(self, task_id: str, job_id: str | None, reason: str) -> None:
        """Reset a failed/stuck task for retry if attempts remain, otherwise fail it."""
        task = await self.db.get_task(task_id)
        if not task:
            return

        # Special handling for tasks stuck at in_review: reset to pr_open
        # so the next engine poll re-launches a reviewer
        if task.status == "in_review":
            try:
                await self.db.update_task(task_id, status=TaskStatus.PR_OPEN, agent_role="")
                logger.info("Task %s reset from in_review to pr_open for review retry", task_id)
            except InvalidTransitionError:
                logger.debug("Could not reset task %s from in_review to pr_open", task_id)
            if job_id:
                await self.db.record_event(job_id, "task_review_retry", "arbiter", f"task={task_id} reason={reason[:200]}")
            return

        if task.attempt >= task.max_attempts:
            logger.warning("Task %s exhausted %d attempts, marking failed", task_id, task.max_attempts)
            try:
                await self.db.update_task(task_id, status=TaskStatus.FAILED, agent_role="", error=f"Exhausted {task.max_attempts} attempts: {reason}")
            except InvalidTransitionError:
                pass
            return

        # First mark the task as failed (required before pending transition)
        try:
            if task.status != "failed":
                await self.db.update_task(task_id, status=TaskStatus.FAILED, agent_role="", error=reason)
        except InvalidTransitionError:
            logger.debug("Could not fail task %s for retry (already transitioned)", task_id)
            return

        # Reset to pending with incremented attempt
        try:
            await self.db.update_task(task_id, status=TaskStatus.PENDING, agent_role="", attempt=task.attempt + 1, error=None)
        except InvalidTransitionError:
            logger.debug("Could not reset task %s to pending for retry", task_id)
            return

        logger.info("Task %s reset to pending for retry (attempt %d/%d)", task_id, task.attempt + 1, task.max_attempts)

        # Reset job to tasks_created so the engine picks up the retried task
        if job_id:
            job = await self.db.get_job(job_id)
            if job and job.status not in ("done", "tasks_created", "spec_received", "spec_ready"):
                try:
                    await self.db.update_job_status(job_id, "tasks_created")
                except InvalidTransitionError:
                    logger.debug("Could not reset job %s to tasks_created for retry", job_id)

            await self.db.record_event(
                job_id, "task_retry_triggered", "arbiter", f"task={task_id} attempt={task.attempt + 1}/{task.max_attempts} reason={reason[:200]}"
            )

    async def _try_advance_stuck_job(self, job_id: str) -> None:
        """Advance a job that is stuck because its tasks have already progressed past it."""
        job = await self.db.get_job(job_id)
        if not job:
            return

        tasks = await self.db.get_tasks(job_id)
        dev_roles = {"backend_engineer", "frontend_engineer", "database_engineer"}
        dev_tasks = [t for t in tasks if t.agent_role in dev_roles]
        if not dev_tasks:
            return

        try:
            if job.status == "review_in_progress":
                all_past_review = all(t.status in ("merged", "done", "failed") for t in dev_tasks)
                if all_past_review:
                    any_merged = any(t.status in ("merged", "done") for t in dev_tasks)
                    if any_merged:
                        await self.db.update_job_status(job_id, "merged")
                        logger.info("Advanced stuck job %s from review_in_progress to merged", job_id)
                    else:
                        await self.db.update_job_status(job_id, "failed", error="All dev tasks failed during review")

            elif job.status == "dev_in_progress":
                terminal = {"merged", "done", "failed"}
                all_terminal = all(t.status in terminal for t in dev_tasks)
                if all_terminal:
                    has_merged = any(t.status in ("merged", "done") for t in dev_tasks)
                    if has_merged:
                        await self.db.update_job_status(job_id, "merged")
                        logger.info("Advanced stuck job %s from dev_in_progress to merged", job_id)
                    else:
                        await self.db.update_job_status(job_id, "failed", error="All dev tasks failed")
                        logger.info("Advanced stuck job %s from dev_in_progress to failed (all tasks failed)", job_id)
        except InvalidTransitionError as e:
            logger.debug("Could not advance stuck job %s: %s", job_id, e)

    # =========================================================================
    # Circuit Breaker
    # =========================================================================

    def _record_failure(self) -> None:
        """Record a failure timestamp for circuit breaker tracking."""
        now = time.time()
        self._failure_timestamps.append(now)
        # Prune old entries outside the window
        cutoff = now - self.timeout_config.circuit_breaker_window_seconds
        while self._failure_timestamps and self._failure_timestamps[0] < cutoff:
            self._failure_timestamps.popleft()

        if len(self._failure_timestamps) >= self.timeout_config.circuit_breaker_failure_threshold:
            self._circuit_open_until = now + self.timeout_config.circuit_breaker_cooldown_seconds
            logger.warning(
                "Circuit breaker OPEN: %d failures in %ds, cooldown %ds",
                len(self._failure_timestamps),
                self.timeout_config.circuit_breaker_window_seconds,
                self.timeout_config.circuit_breaker_cooldown_seconds,
            )

    def _is_circuit_open(self) -> bool:
        """Check if the circuit breaker is currently open."""
        if time.time() < self._circuit_open_until:
            return True
        return False
