"""State machine and agent launch orchestration for jobs."""

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Optional

import httpx

from .agents.dispatch import AgentResultMessage, AgentWorkItem, deserialize_result, serialize_work_item
from .agents.prompt import build_agent_prompt
from .agents.runner import run_agent
from .agents.tools.definitions import get_tools_for_role
from .agents.tools.mcp_executor import create_mcp_tool_executor
from .config import Config
from .connectors.nats_publisher import publish_agent_status, publish_system_event
from .core.models import Agent, AgentRole, Job, JobStatus, Task, TaskStatus, _now
from .core.state_transitions import InvalidTransitionError
from .core.timeout_config import TimeoutConfig
from .db import AbstractDatabase
from .project_registry import ProjectConfig, ServiceTarget, build_registry

if TYPE_CHECKING:
    from .artifact_uploader import ArtifactUploader
    from .connectors.nats_client import NatsClient
    from .providers.k8s import K8sJobLauncher

logger = logging.getLogger(__name__)


class JobEngine:
    def __init__(
        self,
        db: AbstractDatabase,
        config: Config,
        k8s_launcher: "Optional[K8sJobLauncher]" = None,
        nats_client: "Optional[NatsClient]" = None,
        artifact_uploader: "Optional[ArtifactUploader]" = None,
        mcp_server=None,
    ):
        self.db = db
        self.config = config
        self.registry = build_registry(config.projects_file)
        self._running = False
        self._background_tasks: set[asyncio.Task] = set()
        self._k8s_launcher = k8s_launcher
        self._nats_client = nats_client
        self._artifact_uploader = artifact_uploader
        self._mcp_server = mcp_server

    # Dry-run instructions appended to agent prompts when config.dry_run is True
    DRY_RUN_SUFFIX = """

---
## DRY RUN MODE

This is a **dry-run smoke test**. You MUST follow these constraints:
- Do NOT run `git commit`, `git push`, `gh pr create`, or any deploy commands.
- Do NOT write or modify any source code files.
- DO read the codebase, analyze the task, and describe what changes you would make.
- DO call tools to update your task status (update_task_status, report_pr, etc.) so the state machine can advance.
- When reporting PR status, use a placeholder URL like `https://github.com/example/repo/pull/0`.
- Mark your task as done when your analysis is complete.
- Keep your analysis concise (under 500 words).
"""

    async def _nats_agent_status(self, job_id: str, agent_id: str, role: str, status: str):
        """Publish agent status to NATS if enabled. Fire-and-forget."""
        if not self.config.nats_enabled:
            return
        try:
            await publish_agent_status(job_id, agent_id, role, status)
        except Exception:
            logger.debug("NATS publish_agent_status failed for %s", agent_id, exc_info=True)

    async def _nats_system_event(self, job_id: str, event_type: str, source: str, detail: str | None = None):
        """Publish system event to NATS if enabled. Fire-and-forget."""
        if not self.config.nats_enabled:
            return
        try:
            await publish_system_event(job_id, event_type, source, detail)
        except Exception:
            logger.debug("NATS publish_system_event failed for %s", event_type, exc_info=True)

    def _maybe_dry_run(self, prompt: str) -> str:
        """Append dry-run instructions to the prompt if config.dry_run is True."""
        if self.config.dry_run:
            return prompt + self.DRY_RUN_SUFFIX
        return prompt

    async def _trello_comment(self, job: Job, text: str):
        """Post a comment on the Trello card linked to a job, if any."""
        if not job.external_id:
            return
        if not self.config.trello_api_key or not self.config.trello_token:
            return
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                await client.post(
                    f"https://api.trello.com/1/cards/{job.external_id}/actions/comments",
                    params={
                        "key": self.config.trello_api_key,
                        "token": self.config.trello_token,
                        "text": text,
                    },
                )
        except Exception:
            logger.debug("Failed to post Trello comment for job %s", job.id, exc_info=True)

    async def _on_job_terminal(self, job_id: str):
        """Upload artifacts to S3 when a job reaches a terminal state."""
        if not self._artifact_uploader or not self._artifact_uploader.is_enabled():
            return
        try:
            prefix = await self._artifact_uploader.upload_job_artifacts(job_id)
            if prefix:
                bucket = self.config.s3_artifact_bucket
                await self.db.record_event(job_id, "artifacts_uploaded", "job_engine", f"s3://{bucket}/{prefix}")
            else:
                await self.db.record_event(job_id, "artifacts_upload_failed", "job_engine", "uploader returned None")
        except Exception:
            logger.exception("Failed to upload artifacts for job %s", job_id)

    @property
    def _k8s_enabled(self) -> bool:
        return self.config.k8s_dispatch and self._k8s_launcher is not None

    def _resolve_service(self, service_name: str) -> tuple[Optional[ProjectConfig], Optional[ServiceTarget]]:
        """Look up a service target across all registered projects."""
        for project in self.registry.values():
            if project.services:
                svc = project.services.get(service_name)
                if svc:
                    return project, svc
        return None, None

    def _default_working_dir(self) -> str:
        """Return the repo_path of the first registered service, or repo_base_dir."""
        for project in self.registry.values():
            if project.services:
                first_svc = next(iter(project.services.values()), None)
                if first_svc:
                    return first_svc.repo_path
        return self.config.repo_base_dir

    # =========================================================================
    # K8s dispatch
    # =========================================================================

    async def _dispatch_k8s(
        self,
        job: Job,
        agent: Agent,
        role: str,
        prompt: str,
        working_dir: str,
        service: Optional[ServiceTarget] = None,
    ) -> str:
        """Build an AgentWorkItem and launch a K8s Job. Returns the K8s Job name."""
        timeout_cfg = TimeoutConfig()
        role_cfg = timeout_cfg.roles.get(role)
        timeout = role_cfg.task_timeout_seconds if role_cfg else self.config.agent_timeout

        tools = get_tools_for_role(role)
        tool_names = [t["function"]["name"] for t in tools]
        resolved_model = self.config.model
        repo_clone_url = service.clone_url if service else ""

        work_item = AgentWorkItem(
            job_id=job.id,
            agent_id=agent.id,
            role=role,
            prompt=prompt,
            working_dir=working_dir,
            allowed_tools=tool_names,
            mcp_url=f"http://{self.config.mcp_connect_host}:{self.config.mcp_port}/sse",
            timeout=timeout,
            model=resolved_model,
            dry_run=self.config.dry_run,
            env_overrides={},
            repo_clone_url=repo_clone_url,
        )

        k8s_job_name = await self._k8s_launcher.launch_agent(work_item, repo_clone_url)
        await self.db.update_agent(agent.id, status="running", k8s_job_name=k8s_job_name)
        logger.info("Dispatched K8s Job %s for agent %s (role=%s)", k8s_job_name, agent.id, role)
        return k8s_job_name

    async def _on_nats_result(self, msg) -> None:
        """Handle an incoming NATS result message from a K8s Job agent."""
        try:
            result_msg = deserialize_result(msg.data)
            logger.info("Received NATS result for agent %s (success=%s)", result_msg.agent_id, result_msg.success)
            await self._handle_agent_result(result_msg)
        except Exception:
            logger.exception("Error handling NATS agent result")

    async def _handle_agent_result(self, result_msg: AgentResultMessage) -> None:
        """Process an agent result — update DB records and advance state."""
        agent = await self.db.get_agent(result_msg.agent_id)
        if not agent:
            logger.warning("Agent %s not found in DB, ignoring result", result_msg.agent_id)
            return

        if agent.status in ("completed", "failed"):
            logger.debug("Agent %s already %s, ignoring duplicate result", result_msg.agent_id, agent.status)
            return

        job = await self.db.get_job(result_msg.job_id)
        if not job:
            logger.warning("Job %s not found for agent result", result_msg.job_id)
            return

        finished_at = _now()
        usage = {
            "input_tokens": result_msg.input_tokens,
            "output_tokens": result_msg.output_tokens,
            "cache_read_tokens": result_msg.cache_read_tokens,
            "cache_creation_tokens": result_msg.cache_creation_tokens,
            "cost_usd": result_msg.cost_usd,
            "num_turns": result_msg.num_turns,
        }

        role = result_msg.role

        if result_msg.success:
            await self.db.update_agent(agent.id, status="completed", finished_at=finished_at, log_file=result_msg.log_file, **usage)
            await self.db.record_event(job.id, "agent_completed", "engine", f"agent={agent.id} role={role}")
            await self._nats_agent_status(job.id, agent.id, role, "completed")
            await self._trello_comment(job, f"{role} completed (agent={agent.id[:8]}, ${result_msg.cost_usd:.4f})")
        else:
            error_text = result_msg.stderr_tail[:500] if result_msg.stderr_tail else "agent failed"
            await self.db.update_agent(agent.id, status="failed", finished_at=finished_at, error=error_text, log_file=result_msg.log_file, **usage)
            await self.db.record_event(job.id, "agent_failed", "engine", f"agent={agent.id} role={role} error={error_text[:200]}")
            await self._nats_agent_status(job.id, agent.id, role, "failed")
            await self._trello_comment(job, f"{role} failed (agent={agent.id[:8]}): {error_text[:150]}")

            # For non-engineer roles, failure is terminal for the job
            if role in (AgentRole.SPEC_ANALYST, AgentRole.ARBITER):
                await self.db.update_job_status(job.id, JobStatus.FAILED, error=f"{role} failed: {error_text[:200]}")
                await self._on_job_terminal(job.id)
            elif agent.task_id:
                try:
                    await self.db.update_task(agent.task_id, status=TaskStatus.FAILED, error=error_text[:200])
                except InvalidTransitionError:
                    logger.warning("Could not mark task %s as failed", agent.task_id)

    async def _k8s_job_watcher(self) -> None:
        """Fallback watcher: polls K8s Job statuses for Jobs that completed without NATS result."""
        while self._running:
            await asyncio.sleep(30)
            if not self._k8s_launcher:
                continue
            try:
                agent_jobs = await self._k8s_launcher.list_agent_jobs()
                for aj in agent_jobs:
                    if aj["status"] not in ("succeeded", "failed"):
                        continue
                    agent_id = aj.get("agent_id", "")
                    if not agent_id:
                        continue
                    agents = await self.db.get_agents_for_job(aj.get("job_id", ""))
                    matched = None
                    for a in agents:
                        if a.id[:8] == agent_id and a.status not in ("completed", "failed"):
                            matched = a
                            break
                    if not matched:
                        continue

                    logger.warning("K8s Job watcher: detected completed Job %s for agent %s without NATS result", aj["name"], matched.id)

                    logs = ""
                    try:
                        logs = await self._k8s_launcher.get_pod_logs(aj["name"], tail_lines=50)
                    except Exception:
                        logger.debug("Could not fetch pod logs for %s", aj["name"])

                    result_msg = AgentResultMessage(
                        job_id=matched.job_id,
                        agent_id=matched.id,
                        role=str(matched.role),
                        success=(aj["status"] == "succeeded"),
                        return_code=0 if aj["status"] == "succeeded" else 1,
                        log_file="",
                        stderr_tail=logs[:500] if aj["status"] == "failed" else "",
                        input_tokens=0,
                        output_tokens=0,
                        cache_read_tokens=0,
                        cache_creation_tokens=0,
                        cost_usd=0.0,
                        num_turns=0,
                        model="",
                    )
                    await self._handle_agent_result(result_msg)
            except Exception:
                logger.debug("K8s Job watcher poll error", exc_info=True)

        # Cleanup old Jobs on shutdown
        if self._k8s_launcher:
            try:
                await self._k8s_launcher.cleanup_old_jobs()
            except Exception:
                logger.debug("K8s cleanup error", exc_info=True)

    # =========================================================================
    # Lifecycle
    # =========================================================================

    async def start(self):
        """Start the engine polling loop."""
        self._running = True
        logger.info("Job engine started (poll interval: %ds, k8s_dispatch=%s)", self.config.job_engine_poll_interval, self._k8s_enabled)

        # Backfill any terminal jobs that haven't been archived yet
        if self._artifact_uploader and self._artifact_uploader.is_enabled():
            self._spawn(self._run_backfill(), name="artifact-backfill")

        # K8s dispatch: subscribe to NATS results and start Job watcher
        if self._k8s_enabled:
            if self._nats_client:
                await self._nats_client.subscribe_results(self._on_nats_result)
                logger.info("Engine: subscribed to NATS agents.results.> for K8s Job results")
            self._spawn(self._k8s_job_watcher(), name="k8s-job-watcher")

        # Recover from prior unclean shutdown
        await self._startup_cleanup()

        while self._running:
            try:
                await self._poll()
            except Exception:
                logger.exception("Error in engine poll cycle")
            await asyncio.sleep(self.config.job_engine_poll_interval)

    def _spawn(self, coro, name: str) -> asyncio.Task:
        """Wrap an async coroutine as a background task with auto-cleanup."""
        task = asyncio.create_task(coro, name=name)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    async def stop(self):
        self._running = False

        # Mark in-process agents as failed (K8s agents continue independently)
        try:
            running_agents = await self.db.get_running_agents()
            for agent in running_agents:
                if not agent.k8s_job_name:
                    await self.db.update_agent(agent.id, status="failed", finished_at=_now(), error="interrupted by engine shutdown")
            non_k8s = len([a for a in running_agents if not a.k8s_job_name])
            await self.db.record_event(None, "engine_shutdown", "engine", f"agents_interrupted={non_k8s}")
        except Exception:
            logger.debug("Error during graceful shutdown agent cleanup", exc_info=True)

        for task in self._background_tasks:
            task.cancel()
        self._background_tasks.clear()
        logger.info("Job engine stopping")

    # =========================================================================
    # Startup recovery
    # =========================================================================

    async def _startup_cleanup(self):
        """Detect orphaned agents from a prior unclean shutdown and recover their tasks."""
        logger.info("Startup cleanup: checking for orphaned agents...")
        orphaned_count = 0
        recovered_count = 0

        running_agents = await self.db.get_running_agents()
        if not running_agents:
            logger.info("Startup cleanup: no orphaned agents found")
            await self.db.clear_all_heartbeats()
            await self._log_reconciliation()
            return

        for agent in running_agents:
            if not agent.k8s_job_name:
                # In-process agent — process died, so the agent is gone
                await self.db.update_agent(agent.id, status="failed", finished_at=_now(), error="orphaned by restart")
                await self.db.record_event(agent.job_id, "agent_orphaned", "engine", f"agent={agent.id} role={agent.role} reason=restart")
                orphaned_count += 1
            elif self._k8s_enabled and self._k8s_launcher:
                try:
                    k8s_status = await self._k8s_launcher.get_job_status(agent.k8s_job_name)
                except Exception:
                    k8s_status = "unknown"

                if k8s_status in ("succeeded", "failed"):
                    logs = ""
                    try:
                        logs = await self._k8s_launcher.get_pod_logs(agent.k8s_job_name, tail_lines=50)
                    except Exception:
                        pass

                    result_msg = AgentResultMessage(
                        job_id=agent.job_id,
                        agent_id=agent.id,
                        role=str(agent.role),
                        success=(k8s_status == "succeeded"),
                        return_code=0 if k8s_status == "succeeded" else 1,
                        log_file="",
                        stderr_tail=logs[:500] if k8s_status == "failed" else "",
                        input_tokens=0,
                        output_tokens=0,
                        cache_read_tokens=0,
                        cache_creation_tokens=0,
                        cost_usd=0.0,
                        num_turns=0,
                        model="",
                    )
                    await self._handle_agent_result(result_msg)
                    orphaned_count += 1
                elif k8s_status == "unknown":
                    await self.db.update_agent(agent.id, status="failed", finished_at=_now(), error="k8s job not found after restart")
                    await self.db.record_event(
                        agent.job_id, "agent_orphaned", "engine", f"agent={agent.id} role={agent.role} reason=k8s_job_missing"
                    )
                    orphaned_count += 1
                # else: running/pending — leave alone, K8s watcher will handle
            else:
                await self.db.update_agent(agent.id, status="failed", finished_at=_now(), error="orphaned by restart (k8s disabled)")
                await self.db.record_event(agent.job_id, "agent_orphaned", "engine", f"agent={agent.id} role={agent.role} reason=restart")
                orphaned_count += 1

            # Recover the task if the orphaned agent had one
            if agent.task_id:
                task = await self.db.get_task(agent.task_id)
                if not task:
                    continue
                terminal = {TaskStatus.MERGED, TaskStatus.DONE, TaskStatus.FAILED}
                if task.status not in terminal and task.attempt < task.max_attempts:
                    try:
                        await self.db.update_task(task.id, status=TaskStatus.FAILED, error="agent orphaned by restart")
                        await self.db.update_task(task.id, status=TaskStatus.PENDING, attempt=task.attempt + 1)
                        recovered_count += 1
                        logger.info("Startup cleanup: recovered task %s (attempt %d/%d)", task.id, task.attempt + 1, task.max_attempts)
                    except InvalidTransitionError as e:
                        logger.warning("Startup cleanup: could not recover task %s: %s", task.id, e)

        await self.db.clear_all_heartbeats()
        logger.info("Startup cleanup: %d orphaned agents, %d tasks recovered", orphaned_count, recovered_count)
        await self._log_reconciliation()

    async def _log_reconciliation(self):
        """Log a summary of active jobs and their task states after startup cleanup."""
        active_jobs = await self.db.get_active_jobs()
        if not active_jobs:
            logger.info("=== Startup Reconciliation: no active jobs ===")
            return

        lines = [f"=== Startup Reconciliation ({len(active_jobs)} active job(s)) ==="]
        for job in active_jobs:
            tasks = await self.db.get_tasks(job.id)
            if not tasks:
                lines.append(f"  Job {job.id}: {job.status} (no tasks)")
            else:
                status_counts: dict[str, int] = {}
                for t in tasks:
                    status_counts[t.status] = status_counts.get(t.status, 0) + 1
                counts_str = ", ".join(f"{count} {status}" for status, count in sorted(status_counts.items()))
                lines.append(f"  Job {job.id}: {job.status} ({counts_str})")
        logger.info("\n".join(lines))

    # =========================================================================
    # Poll loop
    # =========================================================================

    async def _has_running_agent(self, job_id: str, role: str) -> bool:
        """Check if there is already a running agent of the given role for this job."""
        agents = await self.db.get_agents_for_job(job_id)
        for a in agents:
            if a.role == role and a.status in ("starting", "running"):
                return True
        return False

    async def _poll(self):
        """Check active jobs and advance their state."""
        jobs = await self.db.get_active_jobs()
        for job in jobs:
            try:
                await self._advance(job)
            except Exception:
                logger.exception("Error advancing job %s", job.id)
                await self.db.update_job_status(job.id, JobStatus.FAILED, error="Engine error during advance")
                await self._on_job_terminal(job.id)

    async def _advance(self, job: Job):
        """Advance a job to the next state if conditions are met."""
        try:
            if job.status == JobStatus.SPEC_RECEIVED:
                await self._launch_spec_analyst(job)
            elif job.status == JobStatus.SPEC_READY:
                await self._launch_arbiter(job)
            elif job.status == JobStatus.TASKS_CREATED:
                if job.job_type == "review":
                    await self._launch_review_tasks(job)
                else:
                    await self._launch_engineers(job)
            elif job.status == JobStatus.REVIEW_IN_PROGRESS:
                await self._check_review_tasks(job)
            elif job.status == JobStatus.DEV_IN_PROGRESS:
                await self._manage_dev_tasks(job)
            elif job.status == JobStatus.MERGED:
                await self._launch_deploy_monitor(job)
            elif job.status == JobStatus.DEPLOYING:
                await self._check_deployed(job)
            elif job.status == JobStatus.DEPLOYED:
                await self.db.update_job_status(job.id, JobStatus.DONE)
                await self._nats_system_event(job.id, "job_status_changed", "engine", "status=done")
                await self._on_job_terminal(job.id)
                logger.info("Job %s completed successfully!", job.id)
        except InvalidTransitionError as e:
            logger.warning("Rejected state transition for job %s: %s", job.id, e)
            await self.db.record_event(job.id, "transition_rejected", "engine", str(e))

    # =========================================================================
    # Agent launch helpers (in-process via LiteLLM)
    # =========================================================================

    async def _run_in_process(
        self,
        job: Job,
        task: Task,
        agent: Agent,
        project: Optional[ProjectConfig],
        service: Optional[ServiceTarget],
        context: Optional[str] = None,
    ):
        """Run an agent in-process using the LiteLLM tool-use loop."""
        # Create the appropriate tool executor for non-reviewer roles
        working_dir = "."
        if service and service.repo_path:
            working_dir = service.repo_path
        elif project and project.repo_path:
            working_dir = project.repo_path

        tool_executor = create_mcp_tool_executor(
            mcp_server=self._mcp_server,
            job=job,
            task=task,
            agent_id=agent.id,
            working_dir=working_dir,
        )

        result_agent = await run_agent(
            job=job,
            task=task,
            project=project,
            service=service,
            config=self.config,
            db=self.db,
            tool_executor=tool_executor,
            context=context,
        )

        if result_agent.status == "done":
            await self.db.record_event(job.id, "agent_completed", "engine", f"agent={agent.id} role={task.agent_role}")
            await self._nats_agent_status(job.id, agent.id, str(task.agent_role), "completed")
            await self._trello_comment(job, f"{task.agent_role} completed (agent={agent.id[:8]}, ${result_agent.cost_usd:.4f})")
        else:
            error = result_agent.error or "agent failed"
            await self.db.record_event(
                job.id, "agent_failed", "engine", f"agent={agent.id} role={task.agent_role} error={error[:200]}"
            )
            await self._nats_agent_status(job.id, agent.id, str(task.agent_role), "failed")
            await self._trello_comment(job, f"{task.agent_role} failed (agent={agent.id[:8]}): {error[:150]}")

        return result_agent

    # =========================================================================
    # Checkpoint summary for retries
    # =========================================================================

    async def _build_checkpoint_summary(self, task_id: str) -> str:
        """Assemble a checkpoint summary from persisted subtask results for retry agents."""
        task = await self.db.get_task(task_id)
        if not task:
            return "No task data available."

        parts = []

        if task.branch_name:
            parts.append(f"Branch: `{task.branch_name}`")
        if task.pr_url:
            parts.append(f"Existing PR: {task.pr_url}")

        agents = await self.db.get_agents_for_job(task.job_id)
        task_agents = [a for a in agents if a.task_id == task_id]
        if task_agents:
            parts.append(f"Prior attempts: {len(task_agents)}")
            for a in task_agents:
                status_line = f"  - Agent {a.id[:8]}: status={a.status}, cost=${a.cost_usd:.4f}"
                if a.error:
                    status_line += f", error={a.error[:150]}"
                parts.append(status_line)

        subtasks = await self.db.get_subtasks(task_id)
        if subtasks:
            completed = [s for s in subtasks if s.status == "completed"]
            failed = [s for s in subtasks if s.status == "failed"]
            pending = [s for s in subtasks if s.status in ("pending", "running")]

            if completed:
                parts.append("\nCompleted subtasks:")
                for s in completed:
                    result_str = ""
                    if s.result:
                        truncated = json.dumps(s.result, default=str)
                        if len(truncated) > 300:
                            truncated = truncated[:300] + "..."
                        result_str = f" result={truncated}"
                    parts.append(f"  - [{s.sequence_num}] {s.description}{result_str}")

            if failed:
                parts.append("\nFailed subtasks:")
                for s in failed:
                    error_str = f" error={s.error}" if s.error else ""
                    parts.append(f"  - [{s.sequence_num}] {s.description}{error_str}")

            if pending:
                parts.append("\nRemaining subtasks:")
                for s in pending:
                    parts.append(f"  - [{s.sequence_num}] {s.description}")
        else:
            parts.append("\nNo subtasks were created by the prior agent.")

        return "\n".join(parts)

    # =========================================================================
    # State handlers
    # =========================================================================

    # =========================================================================
    # Review job handlers
    # =========================================================================

    async def _launch_review_tasks(self, job: Job):
        """Launch CODE_REVIEWER tasks for a review-type job."""
        pending_tasks = await self.db.get_tasks_by_status(job.id, TaskStatus.PENDING)
        review_tasks = [t for t in pending_tasks if t.agent_role == AgentRole.CODE_REVIEWER]

        if not review_tasks:
            logger.warning("Review job %s has no pending reviewer tasks", job.id)
            await self.db.update_job_status(job.id, JobStatus.FAILED, error="No reviewer tasks found")
            return

        await self.db.update_job_status(job.id, JobStatus.REVIEW_IN_PROGRESS)

        for task in review_tasks:
            self._spawn(self._run_review_in_process(job, task), name=f"review-{task.id[:8]}")

    async def _run_review_in_process(self, job: Job, task: Task):
        """Run a single review task in-process using the unified agent loop."""
        from .providers.git import create_provider

        # Resolve project config
        project = self.registry.get(task.service)
        if not project:
            # Ad-hoc project — create minimal config
            from .project_registry import ProjectConfig

            provider_hint = "gitlab"
            if task.mr_url and "/pull/" in task.mr_url:
                provider_hint = "github"
            project = ProjectConfig(
                name=task.service,
                project_id="",
                git_provider=provider_hint,
                gitlab_url=self.config.gitlab_url,
                model=self.config.model,
            )

        # Create git provider
        try:
            provider = _create_provider_for_project(project, self.config)
        except ValueError as e:
            logger.error("Failed to create provider for task %s: %s", task.id, e)
            await self.db.update_task(task.id, status=TaskStatus.FAILED, error=str(e)[:200])
            return

        # Transition task to IN_PROGRESS
        try:
            await self.db.update_task(task.id, status=TaskStatus.IN_PROGRESS)
        except InvalidTransitionError as e:
            logger.warning("Could not advance task %s to in_progress: %s", task.id, e)
            return

        # Fetch MR metadata
        mr_info = {}
        try:
            changed_files = await provider.get_changed_files(project.project_id, task.mr_id)
            mr_info = {
                "project_id": project.project_id,
                "changed_files": changed_files,
            }
        except Exception as e:
            logger.warning("Failed to fetch MR info for task %s: %s", task.id, e)
            mr_info = {"project_id": project.project_id, "changed_files": []}

        # Create agent record
        agent = Agent(job_id=job.id, role=AgentRole.CODE_REVIEWER, task_id=task.id, model=self.config.model)
        agent = await self.db.create_agent(agent)

        await self.db.record_event(job.id, "agent_launched", "engine", f"agent={agent.id} role=code_reviewer task={task.id}")
        await self._nats_agent_status(job.id, agent.id, "code_reviewer", "launched")

        # Run the agent
        result_agent = await run_agent(
            job=job,
            task=task,
            project=project,
            config=self.config,
            db=self.db,
            provider=provider,
            mr_info=mr_info,
        )

        # Update task with review results
        if result_agent.status == "done":
            verdict = getattr(result_agent, "_review_verdict", None)
            comments_posted = getattr(result_agent, "_review_comments_posted", 0)
            try:
                await self.db.update_task(
                    task.id,
                    status=TaskStatus.DONE,
                    verdict=verdict,
                    comments_posted=comments_posted,
                )
            except InvalidTransitionError as e:
                logger.warning("Could not mark review task %s as done: %s", task.id, e)

            await self.db.record_event(
                job.id, "review_complete", "engine",
                f"task={task.id} verdict={verdict} comments={comments_posted}",
            )
            await self._nats_agent_status(job.id, agent.id, "code_reviewer", "completed")
        else:
            error = result_agent.error or "unknown"
            try:
                await self.db.update_task(task.id, status=TaskStatus.FAILED, error=error[:200])
            except InvalidTransitionError:
                logger.warning("Could not mark review task %s as failed", task.id)
            await self._nats_agent_status(job.id, agent.id, "code_reviewer", "failed")

    async def _check_review_tasks(self, job: Job):
        """Check if all review tasks are terminal and advance the job."""
        tasks = await self.db.get_tasks(job.id)
        review_tasks = [t for t in tasks if t.agent_role == AgentRole.CODE_REVIEWER]

        if not review_tasks:
            return

        terminal = {TaskStatus.DONE, TaskStatus.FAILED}
        all_terminal = all(t.status in terminal for t in review_tasks)
        if not all_terminal:
            return

        all_failed = all(t.status == TaskStatus.FAILED for t in review_tasks)
        if all_failed:
            await self.db.update_job_status(job.id, JobStatus.FAILED, error="All review tasks failed")
        else:
            await self.db.update_job_status(job.id, JobStatus.DONE)
            logger.info("Review job %s completed", job.id)

    # =========================================================================
    # Development job handlers
    # =========================================================================

    async def _launch_spec_analyst(self, job: Job):
        """Launch the spec analyst agent to refine the raw spec."""
        if await self._has_running_agent(job.id, AgentRole.SPEC_ANALYST):
            return

        # Create a virtual task for the spec analyst
        task = Task(
            job_id=job.id,
            title="Refine specification",
            description=job.spec,
            service="_spec",
            agent_role=AgentRole.SPEC_ANALYST,
            status=TaskStatus.IN_PROGRESS,
        )
        task = await self.db.create_task(task)

        agent = Agent(job_id=job.id, role=AgentRole.SPEC_ANALYST, task_id=task.id, model=self.config.model)
        agent = await self.db.create_agent(agent)

        logger.info("Launching spec analyst for job %s", job.id)
        await self.db.record_event(job.id, "agent_launched", "engine", f"agent={agent.id} role=spec_analyst")
        await self._nats_agent_status(job.id, agent.id, "spec_analyst", "launched")
        await self._trello_comment(job, f"Spec analyst started (agent={agent.id[:8]})")

        if self._k8s_enabled:
            prompt = self._maybe_dry_run(build_agent_prompt(job, task, None, None))
            await self._dispatch_k8s(job, agent, AgentRole.SPEC_ANALYST, prompt, self._default_working_dir())
            return

        result_agent = await self._run_in_process(job, task, agent, None, None)

        if result_agent.status == "done":
            # If arbiter not enabled, check if spec analyst advanced the job
            if not self.config.arbiter_enabled:
                updated_job = await self.db.get_job(job.id)
                if updated_job and updated_job.status == JobStatus.SPEC_RECEIVED:
                    logger.warning("Spec analyst completed but didn't submit refined spec for job %s, advancing with raw spec", job.id)
                    await self.db.update_job_status(job.id, JobStatus.SPEC_READY)
        else:
            await self.db.update_job_status(job.id, JobStatus.FAILED, error=f"Spec analyst failed: {result_agent.error or 'unknown'}")
            await self._on_job_terminal(job.id)

    async def _launch_arbiter(self, job: Job):
        """Launch the arbiter agent to break down the spec into tasks."""
        if await self._has_running_agent(job.id, AgentRole.ARBITER):
            return

        # Re-fetch job to get the refined spec
        job = await self.db.get_job(job.id) or job

        task = Task(
            job_id=job.id,
            title="Create task plan",
            description=job.spec,
            service="_arbiter",
            agent_role=AgentRole.ARBITER,
            status=TaskStatus.IN_PROGRESS,
        )
        task = await self.db.create_task(task)

        agent = Agent(job_id=job.id, role=AgentRole.ARBITER, task_id=task.id, model=self.config.model)
        agent = await self.db.create_agent(agent)

        logger.info("Launching arbiter for job %s", job.id)
        await self.db.record_event(job.id, "agent_launched", "engine", f"agent={agent.id} role=arbiter")
        await self._nats_agent_status(job.id, agent.id, "arbiter", "launched")
        await self._trello_comment(job, f"Arbiter started (agent={agent.id[:8]})")

        if self._k8s_enabled:
            prompt = self._maybe_dry_run(build_agent_prompt(job, task, None, None))
            await self._dispatch_k8s(job, agent, AgentRole.ARBITER, prompt, self._default_working_dir())
            return

        result_agent = await self._run_in_process(job, task, agent, None, None)

        if result_agent.status == "done":
            if not self.config.arbiter_enabled:
                updated_job = await self.db.get_job(job.id)
                if updated_job and updated_job.status == JobStatus.SPEC_READY:
                    tasks = await self.db.get_tasks(job.id)
                    # Filter out the virtual arbiter task itself
                    real_tasks = [t for t in tasks if t.service not in ("_spec", "_arbiter")]
                    if real_tasks:
                        await self.db.update_job_status(job.id, JobStatus.TASKS_CREATED)
                    else:
                        await self.db.update_job_status(job.id, JobStatus.FAILED, error="Arbiter created no tasks")
                        await self._on_job_terminal(job.id)
        else:
            await self.db.update_job_status(job.id, JobStatus.FAILED, error=f"Arbiter failed: {result_agent.error or 'unknown'}")
            await self._on_job_terminal(job.id)

    async def _launch_engineers(self, job: Job):
        """Launch engineering agents in parallel for pending tasks."""
        engineer_roles = {AgentRole.BACKEND_ENGINEER, AgentRole.FRONTEND_ENGINEER, AgentRole.DATABASE_ENGINEER}

        pending_tasks = await self.db.get_tasks_by_status(job.id, TaskStatus.PENDING)
        pending_tasks = [t for t in pending_tasks if t.agent_role in engineer_roles]

        fresh_tasks = [t for t in pending_tasks if t.attempt <= 1]
        retry_tasks = [t for t in pending_tasks if t.attempt > 1]

        # Also pick up tasks needing revisions
        in_progress_tasks = await self.db.get_tasks_by_status(job.id, TaskStatus.IN_PROGRESS)
        revision_tasks = [t for t in in_progress_tasks if t.review_status == "changes_requested" and t.agent_role in engineer_roles]

        all_tasks = fresh_tasks + retry_tasks + revision_tasks
        if not all_tasks:
            all_job_tasks = await self.db.get_tasks(job.id)
            real_tasks = [t for t in all_job_tasks if t.service not in ("_spec", "_arbiter")]
            if not real_tasks:
                logger.info("Job %s has no tasks -- arbiter determined no work needed", job.id)
                await self.db.update_job_status(job.id, JobStatus.NO_WORK_NEEDED)
                await self._trello_comment(job, "No tasks needed -- arbiter determined no changes required.")
                return
            return

        await self.db.update_job_status(job.id, JobStatus.DEV_IN_PROGRESS)

        for t in fresh_tasks:
            self._spawn(self._run_engineer(job, t, is_revision=False), name=f"eng-{t.id[:8]}")
        for t in retry_tasks:
            self._spawn(self._run_engineer(job, t, is_retry=True), name=f"eng-retry-{t.id[:8]}")
        for t in revision_tasks:
            self._spawn(self._run_engineer(job, t, is_revision=True), name=f"eng-rev-{t.id[:8]}")

    async def _run_engineer(self, job: Job, task: Task, is_revision: bool = False, is_retry: bool = False):
        """Run a single engineering agent for a task."""
        project, service = self._resolve_service(task.service)
        if not service:
            logger.error("Unknown service %s for task %s", task.service, task.id)
            await self.db.update_task(task.id, status=TaskStatus.FAILED, error=f"Unknown service: {task.service}")
            return

        context = None

        if is_retry:
            checkpoint_summary = await self._build_checkpoint_summary(task.id)
            prior_error = task.error or "Unknown error"
            branch_name = task.branch_name
            if not branch_name:
                short_job_id = job.id[:8]
                slug = task.title.lower().replace(" ", "-")[:30]
                branch_name = f"feat/job-{short_job_id}/{slug}"

            try:
                await self.db.update_task(task.id, branch_name=branch_name, status=TaskStatus.IN_PROGRESS)
            except InvalidTransitionError as e:
                logger.warning("Rejected task transition for %s: %s", task.id, e)
                return

            context = f"## Retry Context (attempt {task.attempt}/{task.max_attempts})\n\nPrior error: {prior_error}\n\n{checkpoint_summary}"

        elif is_revision:
            branch_name = task.branch_name
            try:
                await self.db.update_task(task.id, review_status="revision_in_progress")
            except InvalidTransitionError as e:
                logger.warning("Rejected task transition for %s: %s", task.id, e)
                return

            review_feedback = await self._get_review_feedback(job.id, task)
            context = f"## Revision Context (revision {task.revision_count})\n\n{review_feedback}"

        else:
            # Fresh task — generate branch name and transition to in_progress
            short_job_id = job.id[:8]
            slug = task.title.lower().replace(" ", "-")[:30]
            branch_name = f"feat/job-{short_job_id}/{slug}"
            try:
                await self.db.update_task(task.id, branch_name=branch_name, status=TaskStatus.IN_PROGRESS)
            except InvalidTransitionError as e:
                logger.warning("Rejected task transition for %s: %s", task.id, e)
                return

        agent = Agent(job_id=job.id, role=task.agent_role, task_id=task.id, model=self.config.model)
        agent = await self.db.create_agent(agent)

        action = "retry" if is_retry else ("revision" if is_revision else "development")
        event_detail = f"agent={agent.id} role={task.agent_role} task={task.id} action={action}"
        await self.db.record_event(job.id, "agent_launched", "engine", event_detail)
        await self._nats_agent_status(job.id, agent.id, str(task.agent_role), "launched")
        await self._trello_comment(job, f"{task.agent_role} started {action} on {task.service} (agent={agent.id[:8]})")

        if self._k8s_enabled:
            prompt = self._maybe_dry_run(build_agent_prompt(job, task, project, service, context))
            await self._dispatch_k8s(job, agent, str(task.agent_role), prompt, service.repo_path, service=service)
            return

        result_agent = await self._run_in_process(job, task, agent, project, service, context)

        if result_agent.status == "done":
            # After a successful revision, ensure the task moves back to PR_OPEN for re-review
            if is_revision:
                current_task = await self.db.get_task(task.id)
                if current_task and current_task.status not in (TaskStatus.PR_OPEN, TaskStatus.MERGED, TaskStatus.DONE):
                    try:
                        await self.db.update_task(task.id, status=TaskStatus.PR_OPEN, review_status="revision_complete")
                        logger.info("Revision agent completed, task %s -> PR_OPEN for re-review", task.id)
                    except InvalidTransitionError as e:
                        logger.warning("Rejected task transition for %s: %s", task.id, e)
        else:
            try:
                await self.db.update_task(task.id, status=TaskStatus.FAILED, error=(result_agent.error or "unknown")[:200])
            except InvalidTransitionError:
                logger.warning("Could not mark task %s as failed", task.id)

    async def _get_review_feedback(self, job_id: str, task: Task) -> str:
        """Fetch review feedback messages for a task from the code_reviewer."""
        messages = await self.db.get_messages(job_id)
        feedback_parts = []
        for msg in messages:
            if msg.from_role == AgentRole.CODE_REVIEWER and msg.to_role == task.agent_role:
                feedback_parts.append(msg.content)
        if feedback_parts:
            return "\n\n---\n\n".join(feedback_parts)
        return "The reviewer requested changes but no specific feedback was found in messages."

    async def _run_task_review(self, job: Job, task: Task):
        """Launch a code reviewer for a single task's PR."""
        review_context = json.dumps(
            {
                "task_id": task.id,
                "title": task.title,
                "service": task.service,
                "pr_number": task.pr_number,
                "pr_url": task.pr_url,
                "branch": task.branch_name,
                "revision_count": task.revision_count,
            },
            indent=2,
        )

        # Create a reviewer task entry
        reviewer_task = Task(
            job_id=job.id,
            title=f"Review PR for {task.title}",
            description=f"Review PR {task.pr_url or 'pending'}",
            service=task.service,
            agent_role=AgentRole.CODE_REVIEWER,
            status=TaskStatus.IN_PROGRESS,
        )
        reviewer_task = await self.db.create_task(reviewer_task)

        agent = Agent(job_id=job.id, role=AgentRole.CODE_REVIEWER, task_id=reviewer_task.id, model=self.config.model)
        agent = await self.db.create_agent(agent)

        project, service = self._resolve_service(task.service)

        await self.db.record_event(job.id, "agent_launched", "engine", f"agent={agent.id} role=code_reviewer task={task.id}")
        await self._nats_agent_status(job.id, agent.id, "code_reviewer", "launched")
        await self._trello_comment(job, f"Code reviewer started for {task.service} (agent={agent.id[:8]})")

        context = f"## Review Target\n\n{review_context}"

        if self._k8s_enabled:
            prompt = self._maybe_dry_run(build_agent_prompt(job, reviewer_task, project, service, context))
            working_dir = service.repo_path if service else "."
            await self._dispatch_k8s(job, agent, AgentRole.CODE_REVIEWER, prompt, working_dir, service=service)
            return

        # Create git provider and MR info so the reviewer gets a working ToolExecutor
        provider = None
        mr_info = {}
        if project and task.pr_url:
            try:
                provider = _create_provider_for_project(project, self.config)
                changed_files = await provider.get_changed_files(project.project_id, task.mr_id or str(task.pr_number or ""))
                mr_info = {"project_id": project.project_id, "changed_files": changed_files}
            except Exception as e:
                logger.warning("Failed to create provider/fetch MR info for task review %s: %s", task.id, e)
                mr_info = {"project_id": project.project_id if project else "", "changed_files": []}

        # Call run_agent directly with provider/mr_info for proper review executor setup
        result_agent = await run_agent(
            job=job,
            task=reviewer_task,
            project=project,
            service=service,
            config=self.config,
            db=self.db,
            provider=provider,
            mr_info=mr_info if provider else None,
            context=context,
        )

        if result_agent.status != "done":
            # Reset original task from in_review back to pr_open for retry
            try:
                await self.db.update_task(task.id, status=TaskStatus.PR_OPEN)
                logger.info("Reviewer failed, task %s reset to PR_OPEN for retry", task.id)
            except InvalidTransitionError:
                logger.warning("Could not reset task %s to PR_OPEN after reviewer failure", task.id)

    async def _manage_dev_tasks(self, job: Job):
        """Per-task lifecycle manager: review launches, revision cycles, job advancement."""
        engineer_roles = {AgentRole.BACKEND_ENGINEER, AgentRole.FRONTEND_ENGINEER, AgentRole.DATABASE_ENGINEER}
        tasks = await self.db.get_tasks(job.id)
        dev_tasks = [t for t in tasks if t.agent_role in engineer_roles]

        if not dev_tasks:
            return

        terminal_statuses = {TaskStatus.MERGED, TaskStatus.DONE, TaskStatus.FAILED}

        for task in dev_tasks:
            if task.status == TaskStatus.PR_OPEN:
                # PR just opened — transition to in_review and spawn reviewer
                try:
                    await self.db.update_task(task.id, status=TaskStatus.IN_REVIEW)
                except InvalidTransitionError as e:
                    logger.warning("Could not transition task %s to in_review: %s", task.id, e)
                    continue
                self._spawn(self._run_task_review(job, task), name=f"review-{task.id[:8]}")

            elif task.status == TaskStatus.PENDING:
                # Task recovered by startup cleanup or arbiter retry — re-launch
                is_retry = task.attempt > 1
                self._spawn(
                    self._run_engineer(job, task, is_retry=is_retry),
                    name=f"eng-{'retry' if is_retry else 'recover'}-{task.id[:8]}",
                )

            elif task.status == TaskStatus.IN_PROGRESS and task.review_status == "changes_requested":
                # Reviewer requested changes — launch revision engineer
                if task.revision_count >= self.config.max_revisions:
                    logger.warning("Task %s hit max revisions (%d), failing", task.id, self.config.max_revisions)
                    try:
                        await self.db.update_task(
                            task.id, status=TaskStatus.FAILED, error=f"Max revisions ({self.config.max_revisions}) exceeded"
                        )
                    except InvalidTransitionError:
                        pass
                    await self.db.record_event(
                        job.id, "task_max_revisions", "engine", f"task={task.id} revision_count={task.revision_count}"
                    )
                    continue
                await self.db.update_task(task.id, revision_count=task.revision_count + 1)
                await self.db.record_event(
                    job.id,
                    "task_revision_requested",
                    "engine",
                    f"task={task.id} revision={task.revision_count + 1}/{self.config.max_revisions}",
                )
                self._spawn(self._run_engineer(job, task, is_revision=True), name=f"eng-rev-{task.id[:8]}")

            elif task.status in (TaskStatus.IN_PROGRESS, TaskStatus.IN_REVIEW):
                # Check if the agent is actually dead (orphaned task)
                latest_agent = await self.db.get_agent_for_task(task.id)
                if latest_agent and latest_agent.status in ("failed", "completed"):
                    if task.status == TaskStatus.IN_REVIEW:
                        try:
                            await self.db.update_task(task.id, status=TaskStatus.PR_OPEN)
                            logger.info("Orphan recovery: task %s reset from in_review to pr_open", task.id)
                        except InvalidTransitionError as e:
                            logger.warning("Orphan recovery: could not reset task %s to pr_open: %s", task.id, e)
                    else:
                        if task.attempt < task.max_attempts:
                            try:
                                await self.db.update_task(task.id, status=TaskStatus.FAILED, error="agent died without completing")
                                await self.db.update_task(task.id, status=TaskStatus.PENDING, attempt=task.attempt + 1)
                                logger.info("Orphan recovery: task %s reset to pending (attempt %d)", task.id, task.attempt + 1)
                            except InvalidTransitionError as e:
                                logger.warning("Orphan recovery: could not reset task %s: %s", task.id, e)
                        else:
                            try:
                                await self.db.update_task(task.id, status=TaskStatus.FAILED, error="max attempts reached after agent death")
                            except InvalidTransitionError:
                                pass

            elif task.status == TaskStatus.FAILED and task.attempt < task.max_attempts:
                # Failed task with retries remaining — auto-retry
                try:
                    await self.db.update_task(task.id, status=TaskStatus.PENDING, attempt=task.attempt + 1, error=None)
                    logger.info("Auto-retry: task %s reset to pending (attempt %d/%d)", task.id, task.attempt + 1, task.max_attempts)
                    await self.db.record_event(
                        job.id, "task_auto_retry", "engine", f"task={task.id} attempt={task.attempt + 1}/{task.max_attempts}"
                    )
                except InvalidTransitionError as e:
                    logger.warning("Auto-retry: could not reset task %s to pending: %s", task.id, e)

        # Check if all dev tasks have reached a terminal state
        all_terminal = all(t.status in terminal_statuses for t in dev_tasks)
        if not all_terminal:
            return

        all_failed = all(t.status == TaskStatus.FAILED for t in dev_tasks)
        if all_failed:
            await self.db.update_job_status(job.id, JobStatus.FAILED, error="All dev tasks failed")
            await self._on_job_terminal(job.id)
            return

        has_merged = any(t.status in (TaskStatus.MERGED, TaskStatus.DONE) for t in dev_tasks)
        if has_merged:
            await self.db.update_job_status(job.id, JobStatus.MERGED)
            logger.info("Job %s: all dev tasks terminal, advancing to MERGED", job.id)
        else:
            await self.db.update_job_status(job.id, JobStatus.FAILED, error="All dev tasks terminal but none merged")
            await self._on_job_terminal(job.id)

    async def _launch_deploy_monitor(self, job: Job):
        """Launch the deploy monitor agent."""
        if await self._has_running_agent(job.id, AgentRole.DEPLOY_MONITOR):
            return

        tasks = await self.db.get_tasks(job.id)
        merged_tasks = [t for t in tasks if t.status == TaskStatus.MERGED]

        # Auto-complete tasks with no deploy target
        for t in list(merged_tasks):
            _, service = self._resolve_service(t.service)
            if service and service.deploy_target == "none":
                logger.info("Task %s (%s) has no deploy target, marking done", t.id, t.service)
                await self.db.update_task(t.id, status=TaskStatus.DONE)
                merged_tasks.remove(t)

        if not merged_tasks:
            engineer_roles = {AgentRole.BACKEND_ENGINEER, AgentRole.FRONTEND_ENGINEER, AgentRole.DATABASE_ENGINEER}
            dev_tasks = [t for t in tasks if t.agent_role in engineer_roles]
            all_done = dev_tasks and all(t.status in (TaskStatus.DONE, TaskStatus.FAILED) for t in dev_tasks)
            if all_done:
                logger.info("Job %s: all tasks already done, skipping deploy monitor", job.id)
                await self.db.update_job_status(job.id, JobStatus.DEPLOYED)
            return

        await self.db.update_job_status(job.id, JobStatus.DEPLOYING)

        for t in merged_tasks:
            await self.db.update_task(t.id, status=TaskStatus.DEPLOYING)

        deploy_task = Task(
            job_id=job.id,
            title="Monitor deployments",
            description=f"Monitor CI/CD for: {', '.join(sorted(set(t.service for t in merged_tasks)))}",
            service="_deploy",
            agent_role=AgentRole.DEPLOY_MONITOR,
            status=TaskStatus.IN_PROGRESS,
        )
        deploy_task = await self.db.create_task(deploy_task)

        agent = Agent(job_id=job.id, role=AgentRole.DEPLOY_MONITOR, task_id=deploy_task.id, model=self.config.model)
        agent = await self.db.create_agent(agent)

        deploy_info = json.dumps(
            [{"task_id": t.id, "title": t.title, "service": t.service, "branch": t.branch_name} for t in merged_tasks], indent=2
        )
        context = f"## Deploy Targets\n\n{deploy_info}"

        await self.db.record_event(job.id, "agent_launched", "engine", f"agent={agent.id} role=deploy_monitor task={deploy_task.id}")
        await self._nats_agent_status(job.id, agent.id, "deploy_monitor", "launched")
        await self._trello_comment(job, f"Deploy monitor started (agent={agent.id[:8]})")

        if self._k8s_enabled:
            prompt = self._maybe_dry_run(build_agent_prompt(job, deploy_task, None, None, context))
            await self._dispatch_k8s(job, agent, AgentRole.DEPLOY_MONITOR, prompt, self._default_working_dir())
            return

        result_agent = await self._run_in_process(job, deploy_task, agent, None, None, context)

        if result_agent.status == "done":
            try:
                await self.db.update_task(deploy_task.id, status=TaskStatus.DONE)
            except InvalidTransitionError:
                logger.warning("Could not mark deploy task %s as done", deploy_task.id)
        else:
            try:
                await self.db.update_task(deploy_task.id, status=TaskStatus.FAILED, error=(result_agent.error or "unknown")[:200])
            except InvalidTransitionError:
                logger.warning("Could not mark deploy task %s as failed", deploy_task.id)

    async def _check_deployed(self, job: Job):
        """Check if all deployments are complete."""
        tasks = await self.db.get_tasks(job.id)
        engineer_roles = {AgentRole.BACKEND_ENGINEER, AgentRole.FRONTEND_ENGINEER, AgentRole.DATABASE_ENGINEER}
        dev_tasks = [t for t in tasks if t.agent_role in engineer_roles]
        deploy_tasks = [t for t in dev_tasks if t.status in (TaskStatus.DEPLOYING, TaskStatus.DONE, TaskStatus.FAILED)]

        if not deploy_tasks:
            return

        all_done = all(t.status in (TaskStatus.DONE, TaskStatus.FAILED) for t in deploy_tasks)
        if all_done:
            any_failed = any(t.status == TaskStatus.FAILED for t in deploy_tasks)
            if any_failed:
                await self.db.update_job_status(job.id, JobStatus.FAILED, error="One or more deployments failed")
                await self._on_job_terminal(job.id)
            else:
                await self.db.update_job_status(job.id, JobStatus.DEPLOYED)
                await self._on_job_terminal(job.id)

    async def _run_backfill(self):
        """Run artifact backfill for terminal jobs that weren't archived, catching all errors."""
        if not self._artifact_uploader:
            return
        try:
            await self._artifact_uploader.backfill()
        except Exception:
            logger.exception("Artifact backfill failed")


def _create_provider_for_project(project, config):
    """Create the appropriate git provider for a project."""
    from .providers.git import create_provider

    provider_type = project.git_provider or config.git_provider

    if provider_type == "gitlab":
        return create_provider(
            "gitlab",
            gitlab_url=project.gitlab_url or config.gitlab_url,
            token=config.gitlab_token,
        )
    if provider_type == "github":
        return create_provider("github", token=config.github_token)

    raise ValueError(f"Unsupported provider: {provider_type}")


# Backward compat alias
WorkflowEngine = JobEngine
