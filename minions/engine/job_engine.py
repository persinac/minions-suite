"""State machine and agent launch orchestration for jobs."""

import asyncio
import logging
from typing import TYPE_CHECKING

import httpx

from ..agents.dispatch import AgentResultMessage, AgentWorkItem, deserialize_result
from ..agents.runner import run_agent
from ..agents.tools.definitions import get_tools_for_role
from ..agents.tools.mcp_executor import create_mcp_tool_executor
from ..config import Config
from ..connectors.nats_publisher import publish_agent_status, publish_system_event
from ..core.models import Agent, AgentRole, Job, JobStatus, Task, TaskStatus, _now
from ..core.state_transitions import InvalidTransitionError
from ..core.timeout_config import TimeoutConfig
from ..db import AbstractDatabase
from ..project_registry import ProjectConfig, ServiceTarget, build_registry
from ..providers.github_app import ensure_token
from ..repos import ensure_checkout
from . import deploy, dev, review
from .job_graph import advance_job_via_graph

if TYPE_CHECKING:
    from ..artifact_uploader import ArtifactUploader
    from ..connectors.nats_client import NatsClient
    from ..providers.k8s import K8sJobLauncher

logger = logging.getLogger(__name__)


class JobEngine:
    def __init__(
        self,
        db: AbstractDatabase,
        config: Config,
        k8s_launcher: K8sJobLauncher | None = None,
        nats_client: NatsClient | None = None,
        artifact_uploader: ArtifactUploader | None = None,
        mcp_server=None,
        memory_store=None,
        tuplespace=None,
        archiver=None,
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
        self._advance_errors: dict[str, int] = {}  # job_id -> consecutive error count
        # Memory system (optional, gated on config.memory_enabled)
        self.memory_store = memory_store
        self.tuplespace = tuplespace
        self.archiver = archiver

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
        """Upload artifacts to S3 and archive memory when a job reaches a terminal state."""
        if self._artifact_uploader and self._artifact_uploader.is_enabled():
            try:
                prefix = await self._artifact_uploader.upload_job_artifacts(job_id)
                if prefix:
                    bucket = self.config.s3_artifact_bucket
                    await self.db.record_event(job_id, "artifacts_uploaded", "job_engine", f"s3://{bucket}/{prefix}")
                else:
                    await self.db.record_event(job_id, "artifacts_upload_failed", "job_engine", "uploader returned None")
            except Exception:
                logger.exception("Failed to upload artifacts for job %s", job_id)

        # Archive L2 facts to L3 knowledge graph (when memory is enabled)
        if self.config.memory_enabled and self.archiver and self.tuplespace and self.memory_store:
            try:
                archived = await self.archiver.archive_job(self.tuplespace, self.memory_store, job_id, self.tuplespace.project)
                if archived > 0:
                    await self.db.record_event(job_id, "memory_archived", "job_engine", f"archived {archived} facts to L3")
            except Exception:
                logger.exception("Failed to archive memory for job %s", job_id)

    @property
    def _k8s_enabled(self) -> bool:
        return self.config.k8s_dispatch and self._k8s_launcher is not None

    def _resolve_service(self, service_name: str) -> tuple[ProjectConfig | None, ServiceTarget | None]:
        """Look up a service target across all registered projects.

        Falls back to the sole service if only one exists across all projects
        (handles LLM hallucinating service names like '_spec').
        """
        for project in self.registry.values():
            if project.services:
                svc = project.services.get(service_name)
                if svc:
                    return project, svc

        # Fallback: if there's exactly one service total, use it
        all_services = []
        for project in self.registry.values():
            if project.services:
                for svc in project.services.values():
                    all_services.append((project, svc))
        if len(all_services) == 1:
            project, svc = all_services[0]
            logger.warning("Service '%s' not found, falling back to sole service '%s'", service_name, svc.name)
            return project, svc

        return None, None

    def _default_working_dir(self) -> str:
        """Return a neutral working directory when no service could be resolved.

        This used to return the first registered service's repo_path, which
        meant an unresolved service name silently pointed the agent at whatever
        project happened to sort first — it had write, commit and push tools,
        so a job for one repo could have opened a PR against an unrelated one.
        Observed live: every project shared the service name 'app', so a
        wallet-api job checked out Flashback-Android.

        An agent with no repo fails obviously; an agent with the wrong repo
        does damage. Prefer the former.
        """
        logger.error(
            "No service resolved for this task — falling back to %s. The agent will have no checkout. "
            "Check that the task's service name matches a key under some project's `services:` in projects.yaml.",
            self.config.repo_base_dir,
        )
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
        service: ServiceTarget | None = None,
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
                    await self.db.update_task(agent.task_id, status=TaskStatus.FAILED, agent_role="", error=error_text[:200])
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
                # Refresh the GitHub App installation token before advancing any
                # job. No-op when App auth is not configured, and cached otherwise
                # — it only calls GitHub inside the token's refresh margin.
                #
                # This is the single point that keeps os.environ["GH_TOKEN"]
                # current, which is how BOTH `gh` call paths get a token: the
                # explicit-env one in providers/git.py:_run_gh, and the
                # ambient-env one in agents/tools/mcp_executor.py that agents use
                # to open PRs. Doing it here rather than per-call-site means a new
                # `gh` invocation added later is covered for free.
                await ensure_token(self.config)
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
        # If LangGraph engine is enabled, attempt checkpoint-based resume first
        if self.config.use_langgraph_engine:
            try:
                from .checkpointer import create_checkpointer
                from .job_graph import resume_from_checkpoint

                checkpointer = await create_checkpointer(self.config)
                active_jobs = await self.db.get_active_jobs()
                for job in active_jobs:
                    resumed = await resume_from_checkpoint(self, job.id, checkpointer)
                    if resumed:
                        logger.info("Startup cleanup: resumed job %s from LangGraph checkpoint", job.id)
            except Exception as e:
                logger.warning("LangGraph checkpoint resume failed, falling back to standard cleanup: %s", e)

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
                    await self.db.record_event(agent.job_id, "agent_orphaned", "engine", f"agent={agent.id} role={agent.role} reason=k8s_job_missing")
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
                        await self.db.update_task(task.id, status=TaskStatus.FAILED, agent_role="", error="agent orphaned by restart")
                        await self.db.update_task(task.id, status=TaskStatus.PENDING, agent_role="", attempt=task.attempt + 1)
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
                self._advance_errors.pop(job.id, None)
            except Exception as exc:
                count = self._advance_errors.get(job.id, 0) + 1
                self._advance_errors[job.id] = count
                if count >= 10:
                    logger.exception("Job %s failed after %d consecutive advance errors", job.id, count)
                    await self.db.update_job_status(job.id, JobStatus.FAILED, error=f"Engine error: {count} consecutive advance failures")
                    await self._on_job_terminal(job.id)
                    self._advance_errors.pop(job.id, None)
                else:
                    logger.warning("Error advancing job %s (attempt %d/10): %s — will retry next poll", job.id, count, exc)

    async def _advance(self, job: Job):
        """Advance a job to the next state if conditions are met."""
        if self.config.use_langgraph_engine:
            try:
                await advance_job_via_graph(self, job)
                return
            except Exception as e:
                logger.warning("LangGraph advance failed for job %s, falling back: %s", job.id, e)
                # Fall through to existing dispatcher

        try:
            if job.status == JobStatus.SPEC_RECEIVED:
                await dev.launch_spec_analyst(self, job)
            elif job.status == JobStatus.SPEC_READY:
                await dev.launch_arbiter(self, job)
            elif job.status == JobStatus.TASKS_CREATED:
                if job.job_type == "review":
                    await review.launch_review_tasks(self, job)
                else:
                    await dev.launch_engineers(self, job)
            elif job.status == JobStatus.REVIEW_IN_PROGRESS:
                await review.check_review_tasks(self, job)
            elif job.status == JobStatus.DEV_IN_PROGRESS:
                await dev.manage_dev_tasks(self, job)
            elif job.status == JobStatus.MERGED:
                await deploy.launch_deploy_monitor(self, job)
            elif job.status == JobStatus.DEPLOYING:
                await deploy.check_deployed(self, job)
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
        project: ProjectConfig | None,
        service: ServiceTarget | None,
        context: str | None = None,
        knowledge_context: str | None = None,
    ):
        """Run an agent in-process using the LiteLLM tool-use loop."""
        # Per-job spend ceiling. The per-agent limit bounds one agent; this is
        # what bounds a job that keeps launching them. Checked before the agent
        # starts, because once litellm is called the money is already spent.
        if self.config.job_cost_limit_usd > 0:
            usage = await self.db.get_job_usage(job.id)
            spent = float(usage.get("total_cost_usd") or 0.0)
            if spent >= self.config.job_cost_limit_usd:
                message = (
                    f"Job {job.id} has spent ${spent:.2f}, at or over its "
                    f"${self.config.job_cost_limit_usd:.2f} limit — refusing to launch {task.agent_role}"
                )
                logger.error(message)
                await self.db.update_task(task.id, status=TaskStatus.FAILED, error=message)
                await self.db.update_job_status(job.id, JobStatus.FAILED, error=message)
                await self.db.record_event(job.id, "job_cost_limit_exceeded", "engine", message)
                return None

        # Create the appropriate tool executor for non-reviewer roles
        working_dir = "."
        if service and service.repo_path:
            working_dir = service.repo_path
        elif project and project.repo_path:
            working_dir = project.repo_path

        # Nothing else creates this checkout on the in-process path — K8s
        # dispatch gets one from its init container, but that path is off here.
        # Without it every file and shell tool runs against a directory that
        # does not exist.
        if working_dir != "." and service and service.clone_url:
            ok = await ensure_checkout(service.clone_url, working_dir, service.default_branch)
            if not ok:
                logger.error(
                    "Could not prepare checkout at %s for task %s — agent would run against an empty directory",
                    working_dir,
                    task.id,
                )

        tool_executor = create_mcp_tool_executor(
            mcp_server=self._mcp_server,
            job=job,
            task=task,
            agent_id=agent.id,
            working_dir=working_dir,
            config=self.config,
            project=project,
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
            agent=agent,
            knowledge_context=knowledge_context,
        )

        if result_agent.status == "done":
            await self.db.record_event(job.id, "agent_completed", "engine", f"agent={agent.id} role={task.agent_role}")
            await self._nats_agent_status(job.id, agent.id, str(task.agent_role), "completed")
            await self._trello_comment(job, f"{task.agent_role} completed (agent={agent.id[:8]}, ${result_agent.cost_usd:.4f})")
        else:
            error = result_agent.error or "agent failed"
            await self.db.record_event(job.id, "agent_failed", "engine", f"agent={agent.id} role={task.agent_role} error={error[:200]}")
            await self._nats_agent_status(job.id, agent.id, str(task.agent_role), "failed")
            await self._trello_comment(job, f"{task.agent_role} failed (agent={agent.id[:8]}): {error[:150]}")

        return result_agent

    async def _run_backfill(self):
        """Run artifact backfill for terminal jobs that weren't archived, catching all errors."""
        if not self._artifact_uploader:
            return
        try:
            await self._artifact_uploader.backfill()
        except Exception:
            logger.exception("Artifact backfill failed")


# Backward compat alias
WorkflowEngine = JobEngine
