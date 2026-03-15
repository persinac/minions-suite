"""FastMCP server exposing review management and job orchestration tools.

Provides external integrations (other MCP clients, dashboards, agents) a way
to trigger reviews, manage jobs/tasks/subtasks, query status, and inspect costs.

When arbiter_enabled is True and a NATS client is set (via set_nats_client()),
state-mutating operations route through the Arbiter via NATS request/reply
instead of writing directly to the DB. This ensures centralized transition
validation, circuit breaking, and anomaly detection.
"""

import json
import logging
from pathlib import Path

import httpx
from fastmcp import FastMCP

from ..config import Config
from ..core.models import AgentRole, Job, JobStatus, Message, Subtask, SubtaskStatus, Task, TaskStatus, _now
from ..core.state_transitions import InvalidTransitionError, PreconditionError
from ..db import AbstractDatabase

logger = logging.getLogger(__name__)

# Module-level NATS client reference, set by CLI when arbiter_enabled.
# When set, state-mutating tools route through the Arbiter.
_nats_client = None


VALID_ROLES = [r.value for r in AgentRole]
VALID_ROLES_STR = ", ".join(VALID_ROLES)


def _resolve_role(raw: str) -> AgentRole:
    """Best-effort resolution of a role string to AgentRole.

    Handles dashes, underscores, and common hallucinated names.
    """
    normalized = raw.strip().lower().replace("-", "_")

    # Direct match
    try:
        return AgentRole(normalized)
    except ValueError:
        pass

    # Common hallucinated names
    if "spec" in normalized or "analyst" in normalized or "product" in normalized:
        return AgentRole.SPEC_ANALYST
    if "database" in normalized or "migration" in normalized or "schema" in normalized:
        return AgentRole.DATABASE_ENGINEER
    if "backend" in normalized or "api" in normalized:
        return AgentRole.BACKEND_ENGINEER
    if "frontend" in normalized or "dashboard" in normalized or "store" in normalized:
        return AgentRole.FRONTEND_ENGINEER
    if "review" in normalized:
        return AgentRole.CODE_REVIEWER
    if "deploy" in normalized or "monitor" in normalized:
        return AgentRole.DEPLOY_MONITOR
    if "orchestrat" in normalized or "arbiter" in normalized:
        return AgentRole.ARBITER
    if normalized in ("engineer", "developer", "dev"):
        return AgentRole.BACKEND_ENGINEER

    raise ValueError(f"'{raw}' is not a valid role. Valid roles: {VALID_ROLES_STR}")


def set_nats_client(nats_client) -> None:
    """Set the module-level NATS client for arbiter-routed transitions.

    Called by cli.py when arbiter_enabled=True and NATS is connected.
    """
    global _nats_client
    _nats_client = nats_client


async def _propose_transition(entity_type: str, entity_id: str, to_status: str, job_id: str | None = None, **kwargs) -> dict:
    """Route a state transition through the Arbiter via NATS request/reply.

    Raises InvalidTransitionError if the Arbiter rejects the transition.
    """
    response = await _nats_client.request(
        "arbiter.state.transition",
        {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "to_status": to_status,
            "job_id": job_id,
            "kwargs": kwargs,
        },
        timeout=10.0,
    )
    if not response.get("approved"):
        raise InvalidTransitionError(
            entity_type,
            entity_id,
            response.get("from_status", "?"),
            to_status,
        )
    return response


async def _label_mr(config: Config | None, job_id: str, service: str, pr_url: str, pr_number: int | None) -> None:
    """Add a minions-job-<id> label to the MR so webhooks can skip it."""
    import re

    from ..project_registry import build_registry
    from ..providers.git import create_provider

    if not config or not pr_url:
        return

    mr_id = str(pr_number or "")
    if not mr_id:
        match = re.search(r"/merge_requests/(\d+)", pr_url)
        if not match:
            match = re.search(r"/pull/(\d+)", pr_url)
        if match:
            mr_id = match.group(1)
    if not mr_id:
        return

    try:
        registry = build_registry(config.projects_file)
        project = registry.get(service)

        provider_type = (project.git_provider if project else None) or config.git_provider
        project_id = project.project_id if project else ""

        # Fall back to extracting project_id from the URL
        if not project_id:
            match = re.search(r"gitlab\.com/(.+?)/-/merge_requests/", pr_url)
            if match:
                project_id = match.group(1)

        if not project_id:
            return

        if provider_type == "gitlab":
            provider = create_provider("gitlab", gitlab_url=(project.gitlab_url if project else "") or config.gitlab_url, token=config.gitlab_token)
        elif provider_type == "github":
            provider = create_provider("github", token=config.github_token)
        else:
            return

        labels = [f"minions-job-{job_id[:8]}"]
        result = await provider.add_mr_labels(project_id, mr_id, labels)
        if result.get("labels_added"):
            logger.info("Labeled MR %s with %s", pr_url, labels)
        else:
            logger.warning("Failed to label MR %s: %s", pr_url, result.get("error", "unknown"))
    except Exception as e:
        logger.warning("Could not label MR %s: %s", pr_url, e)


def create_server(db: AbstractDatabase, config: Config | None = None, tuplespace=None, memory_enabled: bool = False) -> FastMCP:
    """Create and return the FastMCP server with review + job orchestration tools."""
    mcp = FastMCP("Minion Suite", instructions="AI agent suite — composable, vendor-agnostic agents. Code review + multi-agent job orchestration.")

    from .middleware import ToolAuditMiddleware

    mcp.add_middleware(ToolAuditMiddleware(db=db))

    # =========================================================================
    # Review Tools (via Job/Task infrastructure)
    # =========================================================================

    @mcp.tool()
    async def request_review(project: str, mr_url: str, mr_id: str) -> str:
        """Queue a new code review by creating a review-type job with a CODE_REVIEWER task."""
        job, task = await db.create_review_job(project, mr_url, mr_id)
        return json.dumps({"job_id": job.id, "task_id": task.id, "status": str(job.status), "project": project})

    @mcp.tool()
    async def get_review_status(job_id: str) -> str:
        """Get the current status of a review job and its tasks."""
        job = await db.get_job(job_id)
        if not job:
            return json.dumps({"error": f"Job {job_id} not found"})
        tasks = await db.get_tasks(job_id)
        review_tasks = [t for t in tasks if t.agent_role == AgentRole.CODE_REVIEWER]
        task = review_tasks[0] if review_tasks else None
        return json.dumps(
            {
                "job_id": job.id,
                "status": str(job.status),
                "mr_url": job.mr_url,
                "verdict": task.verdict if task else None,
                "comments_posted": task.comments_posted if task else 0,
                "error": job.error,
            }
        )

    @mcp.tool()
    async def get_review_history(project: str | None = None, limit: int = 20) -> str:
        """Get recent review history, optionally filtered by project."""
        all_jobs = await db.get_all_jobs()
        review_jobs = [j for j in all_jobs if j.job_type == "review"]
        if project:
            filtered = []
            for j in review_jobs:
                tasks = await db.get_tasks(j.id)
                if any(t.service == project for t in tasks):
                    filtered.append(j)
            review_jobs = filtered
        review_jobs = review_jobs[:limit]

        results = []
        for j in review_jobs:
            tasks = await db.get_tasks(j.id)
            review_task = next((t for t in tasks if t.agent_role == AgentRole.CODE_REVIEWER), None)
            results.append(
                {
                    "job_id": j.id,
                    "project": review_task.service if review_task else "",
                    "mr_url": j.mr_url or "",
                    "status": str(j.status),
                    "verdict": review_task.verdict if review_task else None,
                    "comments_posted": review_task.comments_posted if review_task else 0,
                    "created_at": j.created_at,
                }
            )
        return json.dumps(results)

    @mcp.tool()
    async def cancel_review(job_id: str) -> str:
        """Cancel a pending review job."""
        job = await db.get_job(job_id)
        if not job:
            return json.dumps({"error": f"Job {job_id} not found"})
        if job.job_type != "review":
            return json.dumps({"error": f"Job {job_id} is not a review job"})
        terminal = {JobStatus.DONE, JobStatus.FAILED, JobStatus.NO_WORK_NEEDED}
        if job.status in terminal:
            return json.dumps({"error": f"Job already in terminal state: {job.status}"})
        # Fail all pending tasks
        tasks = await db.get_tasks(job_id)
        for t in tasks:
            if t.status not in {TaskStatus.DONE, TaskStatus.FAILED}:
                try:
                    await db.update_task(t.id, status=TaskStatus.FAILED, error="Cancelled by user")
                except Exception:
                    pass
        try:
            await db.update_job_status(job_id, JobStatus.FAILED, error="Cancelled by user")
        except Exception:
            pass
        return json.dumps({"job_id": job_id, "status": "cancelled"})

    # =========================================================================
    # Cost & Stats Tools
    # =========================================================================

    @mcp.tool()
    async def get_cost_summary(project: str | None = None, days: int = 30) -> str:
        """Get cost and usage summary for reviews over a time period."""
        summary = await db.get_cost_summary(project=project, days=days)
        return json.dumps(summary)

    @mcp.tool()
    async def get_agent_logs(job_id: str) -> str:
        """List agent invocations for a job with their metrics."""
        agents = await db.get_agents_for_job(job_id)
        return json.dumps(
            [
                {
                    "id": a.id,
                    "model": a.model,
                    "status": a.status,
                    "input_tokens": a.input_tokens,
                    "output_tokens": a.output_tokens,
                    "cost_usd": a.cost_usd,
                    "num_turns": a.num_turns,
                    "log_file": a.log_file,
                    "started_at": a.started_at,
                    "finished_at": a.finished_at,
                    "error": a.error,
                }
                for a in agents
            ]
        )

    # =========================================================================
    # Job Management Tools
    # =========================================================================

    @mcp.tool()
    async def submit_spec(spec: str, external_id: str | None = None) -> str:
        """Submit a new feature specification to start a job."""
        job = Job(spec=spec, external_id=external_id)
        job = await db.create_job(job)
        return json.dumps({"job_id": job.id, "status": str(job.status)})

    @mcp.tool()
    async def submit_refined_spec(job_id: str, spec: str) -> str:
        """Submit a refined/structured spec for a job (called by spec analyst agent)."""
        try:
            if _nats_client:
                await _propose_transition("job", job_id, "spec_ready", job_id=job_id, refined_spec=spec)
            else:
                await db.update_job_spec(job_id, spec)
                await db.update_job_status(job_id, JobStatus.SPEC_READY)
            await db.record_event(job_id, "spec_refined", "spec_analyst")
            return json.dumps({"job_id": job_id, "status": "spec_ready"})
        except InvalidTransitionError as e:
            return json.dumps({"error": str(e)})

    @mcp.tool()
    async def create_task(
        job_id: str,
        title: str,
        description: str,
        service: str,
        agent_role: str,
    ) -> str:
        """Create a development task for a specific service within a job.

        The 'service' parameter MUST be a valid service name from the project's
        services configuration (e.g. 'api', 'frontend'). Do NOT use internal
        names like '_spec' or '_arbiter'.
        """
        # Reject reserved/internal service names
        RESERVED_SERVICES = {"_spec", "_arbiter", "_worker"}
        if service.strip().lower() in RESERVED_SERVICES:
            logger.warning("create_task: rejected reserved service name '%s' for title='%s'", service, title)
            return json.dumps(
                {"error": f"'{service}' is a reserved internal service name. Use a real service name from Available Services (e.g. 'api')."}
            )

        resolved_role = _resolve_role(agent_role)
        # Hard guard: database service must always use database_engineer
        if service.strip().lower() == "database" and resolved_role != AgentRole.DATABASE_ENGINEER:
            logger.warning("create_task: overriding role %s -> database_engineer for service=database", resolved_role)
            resolved_role = AgentRole.DATABASE_ENGINEER
        task = Task(
            job_id=job_id,
            title=title,
            description=description,
            service=service,
            agent_role=resolved_role,
        )
        task = await db.create_task(task)
        await db.record_event(job_id, "task_created", "arbiter", f"task={task.id} service={service} role={resolved_role}")
        return json.dumps({"task_id": task.id, "job_id": job_id, "status": str(task.status)})

    @mcp.tool()
    async def mark_tasks_created(job_id: str) -> str:
        """Signal that the arbiter has finished creating all tasks for this job."""
        try:
            if _nats_client:
                await _propose_transition("job", job_id, "tasks_created", job_id=job_id)
            else:
                await db.update_job_status(job_id, JobStatus.TASKS_CREATED)
            await db.record_event(job_id, "tasks_created", "arbiter")
            return json.dumps({"job_id": job_id, "status": "tasks_created"})
        except InvalidTransitionError as e:
            return json.dumps({"error": str(e)})

    @mcp.tool()
    async def get_job_status(job_id: str) -> str:
        """Get the current status of a job and all its tasks."""
        job = await db.get_job(job_id)
        if not job:
            return json.dumps({"error": f"Job {job_id} not found"})
        tasks = await db.get_tasks(job_id)
        return json.dumps(
            {
                "job_id": job.id,
                "status": str(job.status),
                "error": job.error,
                "created_at": job.created_at,
                "updated_at": job.updated_at,
                "tasks": [
                    {
                        "id": t.id,
                        "title": t.title,
                        "service": t.service,
                        "agent_role": str(t.agent_role),
                        "status": str(t.status),
                        "pr_url": t.pr_url,
                        "attempt": t.attempt,
                        "error": t.error,
                    }
                    for t in tasks
                ],
            }
        )

    # =========================================================================
    # Task Status Tools
    # =========================================================================

    @mcp.tool()
    async def update_task_status(task_id: str, status: str, error: str | None = None) -> str:
        """Update the status of a task (called by engineering agents)."""
        try:
            if _nats_client:
                task = await db.get_task(task_id)
                if not task:
                    return json.dumps({"error": f"Task {task_id} not found"})
                kwargs = {}
                if error:
                    kwargs["error"] = error
                await _propose_transition("task", task_id, status, job_id=task.job_id, **kwargs)
                # Re-fetch after arbiter applies the transition
                task = await db.get_task(task_id)
            else:
                update_kwargs = {"status": status}
                if error:
                    update_kwargs["error"] = error
                task = await db.update_task(task_id, **update_kwargs)
            if not task:
                return json.dumps({"error": f"Task {task_id} not found"})
            return json.dumps({"task_id": task_id, "status": str(task.status)})
        except InvalidTransitionError as e:
            return json.dumps({"error": str(e)})
        except PreconditionError as e:
            return json.dumps({"error": str(e), "missing_fields": e.missing_fields})

    @mcp.tool()
    async def report_pr(task_id: str, pr_url: str, pr_number: int, branch_name: str) -> str:
        """Report that a PR has been opened for a task."""
        try:
            if _nats_client:
                task = await db.get_task(task_id)
                if not task:
                    return json.dumps({"error": f"Task {task_id} not found"})
                await _propose_transition(
                    "task",
                    task_id,
                    "pr_open",
                    job_id=task.job_id,
                    pr_url=pr_url,
                    pr_number=pr_number,
                )
                # Update non-state fields directly (branch_name is not a state transition concern)
                await db.update_task(task_id, branch_name=branch_name)
                task = await db.get_task(task_id)
            else:
                task = await db.update_task(task_id, pr_url=pr_url, pr_number=pr_number, branch_name=branch_name, status=TaskStatus.PR_OPEN)
            if not task:
                return json.dumps({"error": f"Task {task_id} not found"})

            if task.job_id:
                await db.record_event(task.job_id, "pr_opened", str(task.agent_role), f"task={task_id} pr={pr_url}")

                # Label the MR so webhooks can ignore minions-created MRs
                await _label_mr(config, task.job_id, task.service, pr_url, pr_number)

            return json.dumps({"task_id": task_id, "status": "pr_open", "pr_url": pr_url})
        except InvalidTransitionError as e:
            return json.dumps({"error": str(e)})

    @mcp.tool()
    async def report_review_complete(task_id: str, verdict: str, feedback: str | None = None) -> str:
        """Report that a PR review is complete (approved or changes_requested)."""
        try:
            if verdict == "approve":
                new_status = "merged"
                review_status = "approved"
            elif verdict == "changes_requested":
                new_status = "in_progress"
                review_status = "changes_requested"
            else:
                return json.dumps({"error": f"Invalid verdict: {verdict}. Use 'approve' or 'changes_requested'"})

            if _nats_client:
                task = await db.get_task(task_id)
                if not task:
                    return json.dumps({"error": f"Task {task_id} not found"})
                await _propose_transition(
                    "task",
                    task_id,
                    new_status,
                    job_id=task.job_id,
                    review_status=review_status,
                    agent_role="code_reviewer",
                )
                task = await db.get_task(task_id)
            else:
                if verdict == "approve":
                    task = await db.update_task(task_id, status=TaskStatus.MERGED, review_status="approved")
                else:
                    task = await db.update_task(task_id, status=TaskStatus.IN_PROGRESS, review_status="changes_requested")

            if not task:
                return json.dumps({"error": f"Task {task_id} not found"})

            # Store feedback as a message if provided
            if feedback and task.job_id:
                msg = Message(
                    job_id=task.job_id,
                    from_role="code_reviewer",
                    to_role=str(task.agent_role),
                    content=feedback,
                )
                await db.send_message(msg)

            if task.job_id:
                await db.record_event(task.job_id, "review_complete", "code_reviewer", f"task={task_id} verdict={verdict}")

            return json.dumps({"task_id": task_id, "verdict": verdict, "status": str(task.status)})
        except InvalidTransitionError as e:
            return json.dumps({"error": str(e)})

    @mcp.tool()
    async def report_deploy_status(task_id: str, status: str, detail: str | None = None) -> str:
        """Report deployment status for a task (deployed or failed)."""
        try:
            if status == "deployed":
                new_status = "done"
                deploy_status = "deployed"
            elif status == "failed":
                new_status = "failed"
                deploy_status = "failed"
            else:
                return json.dumps({"error": f"Invalid deploy status: {status}. Use 'deployed' or 'failed'"})

            if _nats_client:
                task = await db.get_task(task_id)
                if not task:
                    return json.dumps({"error": f"Task {task_id} not found"})
                kwargs = {"deploy_status": deploy_status}
                if status == "failed":
                    kwargs["error"] = detail or "deploy failed"
                await _propose_transition("task", task_id, new_status, job_id=task.job_id, **kwargs)
                task = await db.get_task(task_id)
            else:
                if status == "deployed":
                    task = await db.update_task(task_id, status=TaskStatus.DONE, deploy_status="deployed")
                else:
                    task = await db.update_task(task_id, status=TaskStatus.FAILED, deploy_status="failed", error=detail or "deploy failed")

            if not task:
                return json.dumps({"error": f"Task {task_id} not found"})

            if task.job_id:
                await db.record_event(task.job_id, "deploy_status", "deploy_monitor", f"task={task_id} status={status} detail={detail or ''}")

            return json.dumps({"task_id": task_id, "deploy_status": status})
        except InvalidTransitionError as e:
            return json.dumps({"error": str(e)})

    # =========================================================================
    # Subtask Management Tools
    # =========================================================================

    @mcp.tool()
    async def submit_subtask_plan(task_id: str, subtasks: list[dict]) -> str:
        """Submit a plan of subtasks for a task. Each subtask dict needs a 'description' key."""
        created = []
        for i, st_data in enumerate(subtasks):
            subtask = Subtask(
                task_id=task_id,
                sequence_num=i + 1,
                description=st_data.get("description", f"Subtask {i + 1}"),
            )
            subtask = await db.create_subtask(subtask)
            created.append({"id": subtask.id, "seq": subtask.sequence_num, "description": subtask.description})
        return json.dumps({"task_id": task_id, "subtasks_created": len(created), "subtasks": created})

    @mcp.tool()
    async def start_subtask(subtask_id: str) -> str:
        """Mark a subtask as running."""
        try:
            if _nats_client:
                await _propose_transition("subtask", subtask_id, "running")
                subtask = await db.get_subtask(subtask_id)
            else:
                subtask = await db.update_subtask(subtask_id, status=SubtaskStatus.RUNNING, started_at=_now())
            if not subtask:
                return json.dumps({"error": f"Subtask {subtask_id} not found"})
            return json.dumps({"subtask_id": subtask_id, "status": "running"})
        except InvalidTransitionError as e:
            return json.dumps({"error": str(e)})

    @mcp.tool()
    async def complete_subtask(subtask_id: str, result: str | None = None) -> str:
        """Mark a subtask as completed with optional result."""
        try:
            if _nats_client:
                kwargs = {}
                if result:
                    kwargs["result"] = result
                await _propose_transition("subtask", subtask_id, "completed", **kwargs)
                subtask = await db.get_subtask(subtask_id)
            else:
                subtask = await db.update_subtask(subtask_id, status=SubtaskStatus.COMPLETED, completed_at=_now(), result=result)
            if not subtask:
                return json.dumps({"error": f"Subtask {subtask_id} not found"})
            return json.dumps({"subtask_id": subtask_id, "status": "completed"})
        except InvalidTransitionError as e:
            return json.dumps({"error": str(e)})

    @mcp.tool()
    async def fail_subtask(subtask_id: str, error: str) -> str:
        """Mark a subtask as failed with error message."""
        try:
            if _nats_client:
                await _propose_transition("subtask", subtask_id, "failed", error=error)
                subtask = await db.get_subtask(subtask_id)
            else:
                subtask = await db.update_subtask(subtask_id, status=SubtaskStatus.FAILED, completed_at=_now(), error=error)
            if not subtask:
                return json.dumps({"error": f"Subtask {subtask_id} not found"})
            return json.dumps({"subtask_id": subtask_id, "status": "failed"})
        except InvalidTransitionError as e:
            return json.dumps({"error": str(e)})

    @mcp.tool()
    async def get_subtasks(task_id: str) -> str:
        """Get all subtasks for a task, ordered by sequence number."""
        subtasks = await db.get_subtasks(task_id)
        return json.dumps(
            [
                {
                    "id": s.id,
                    "seq": s.sequence_num,
                    "description": s.description,
                    "status": str(s.status),
                    "started_at": s.started_at,
                    "completed_at": s.completed_at,
                    "result": s.result,
                    "error": s.error,
                }
                for s in subtasks
            ]
        )

    # =========================================================================
    # Communication Tools
    # =========================================================================

    @mcp.tool()
    async def send_message(job_id: str, from_role: str, content: str, to_role: str | None = None) -> str:
        """Send a message to another agent (or broadcast if to_role is None)."""
        msg = Message(job_id=job_id, from_role=from_role, to_role=to_role, content=content)
        msg = await db.send_message(msg)
        return json.dumps({"message_id": msg.id, "from": from_role, "to": to_role or "broadcast"})

    @mcp.tool()
    async def get_messages(job_id: str, role: str | None = None) -> str:
        """Get messages for a job, optionally filtered to a specific role."""
        messages = await db.get_messages(job_id, role=role)
        return json.dumps(
            [
                {
                    "id": m.id,
                    "from_role": m.from_role,
                    "to_role": m.to_role,
                    "content": m.content,
                    "created_at": m.created_at,
                }
                for m in messages
            ]
        )

    # =========================================================================
    # Heartbeat Tool
    # =========================================================================

    @mcp.tool()
    async def send_heartbeat(
        agent_id: str,
        agent_role: str,
        job_id: str | None = None,
        current_task_id: str | None = None,
        current_subtask_id: str | None = None,
    ) -> str:
        """Send a heartbeat signal to indicate an agent is alive."""
        if _nats_client:
            try:
                await _nats_client.publish(
                    "arbiter.heartbeat",
                    {
                        "agent_id": agent_id,
                        "agent_role": agent_role,
                        "job_id": job_id,
                        "current_task_id": current_task_id,
                        "current_subtask_id": current_subtask_id,
                    },
                )
            except Exception:
                logger.debug("Failed to publish heartbeat via NATS", exc_info=True)
        else:
            try:
                await db.upsert_heartbeat(agent_id, agent_role, job_id, current_task_id, current_subtask_id)
            except Exception as e:
                return json.dumps({"error": str(e)})
        return json.dumps({"status": "ok"})

    # =========================================================================
    # Trello Tools
    # =========================================================================

    @mcp.tool()
    async def create_trello_tech_debt(job_id: str, title: str, description: str) -> str:
        """Create a Trello card in the minions-on-deck list for tech debt follow-up."""
        cfg = config or Config.from_env()
        if not cfg.trello_api_key or not cfg.trello_token or not cfg.trello_board_id:
            return json.dumps({"error": "Trello credentials not configured, skipping tech debt card creation"})

        auth_params = {"key": cfg.trello_api_key, "token": cfg.trello_token}
        trello_api = "https://api.trello.com/1"

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(
                    f"{trello_api}/boards/{cfg.trello_board_id}/lists",
                    params={**auth_params, "fields": "name"},
                )
                resp.raise_for_status()
                lists = resp.json()

                list_id = None
                for lst in lists:
                    if lst["name"].strip().lower() == "minions-on-deck":
                        list_id = lst["id"]
                        break

                if not list_id:
                    return json.dumps({"error": "Could not find 'minions-on-deck' list on board"})

                card_desc = f"{description}\n\n---\n_Tech debt from job `{job_id}`_"
                resp = await client.post(
                    f"{trello_api}/cards",
                    params={**auth_params, "idList": list_id, "name": title, "desc": card_desc},
                )
                resp.raise_for_status()
                card = resp.json()
                card_id = card["id"]
                card_url = card.get("shortUrl", card.get("url", ""))

                # Add minion label
                resp = await client.get(
                    f"{trello_api}/boards/{cfg.trello_board_id}/labels",
                    params={**auth_params, "fields": "name"},
                )
                resp.raise_for_status()
                for label in resp.json():
                    if label.get("name", "").strip().lower() == "minion":
                        await client.post(
                            f"{trello_api}/cards/{card_id}/idLabels",
                            params={**auth_params, "value": label["id"]},
                        )
                        break

                await client.post(
                    f"{trello_api}/cards/{card_id}/actions/comments",
                    params={**auth_params, "text": f"Tech debt created from job `{job_id}`"},
                )

                logger.info("Created tech debt Trello card: %s", card_url)
                return json.dumps({"card_id": card_id, "url": card_url})

        except httpx.HTTPError as e:
            logger.error("Trello API error creating tech debt card: %s", e)
            return json.dumps({"error": f"Trello API error: {e}"})
        except Exception as e:
            logger.error("Unexpected error creating tech debt card: %s", e)
            return json.dumps({"error": f"Unexpected error: {e}"})

    @mcp.tool()
    async def create_phase_card(job_id: str, title: str, description: str, phase_number: int) -> str:
        """Create a Trello card in the On-deck list for a phase of a decomposed spec."""
        cfg = config or Config.from_env()
        if not cfg.trello_api_key or not cfg.trello_token or not cfg.trello_board_id:
            return json.dumps({"error": "Trello credentials not configured"})

        job = await db.get_job(job_id)
        if not job:
            return json.dumps({"error": f"Job {job_id} not found"})

        auth_params = {"key": cfg.trello_api_key, "token": cfg.trello_token}
        trello_api = "https://api.trello.com/1"

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(
                    f"{trello_api}/boards/{cfg.trello_board_id}/lists",
                    params={**auth_params, "fields": "name"},
                )
                resp.raise_for_status()
                lists = resp.json()

                list_id = None
                for lst in lists:
                    if lst["name"].strip().lower() == "on-deck":
                        list_id = lst["id"]
                        break

                if not list_id:
                    return json.dumps({"error": "Could not find 'On-deck' list on board"})

                card_name = f"Phase {phase_number}: {title}"
                resp = await client.post(
                    f"{trello_api}/cards",
                    params={**auth_params, "idList": list_id, "name": card_name, "desc": description},
                )
                resp.raise_for_status()
                card = resp.json()
                card_id = card["id"]
                card_url = card.get("shortUrl", card.get("url", ""))

                original_card_id = job.external_id or "unknown"
                await client.post(
                    f"{trello_api}/cards/{card_id}/actions/comments",
                    params={**auth_params, "text": f"Phase {phase_number} decomposed from job `{job_id}` (original card: {original_card_id})"},
                )

                logger.info("Created phase %d Trello card: %s", phase_number, card_url)
                return json.dumps({"card_id": card_id, "url": card_url, "phase_number": phase_number})

        except httpx.HTTPError as e:
            logger.error("Trello API error creating phase card: %s", e)
            return json.dumps({"error": f"Trello API error: {e}"})
        except Exception as e:
            logger.error("Unexpected error creating phase card: %s", e)
            return json.dumps({"error": f"Unexpected error: {e}"})

    @mcp.tool()
    async def mark_phases_created(job_id: str, phase_count: int) -> str:
        """Signal that the spec analyst has decomposed the spec into phase cards. Archives the original Trello card and completes the job."""
        job = await db.get_job(job_id)
        if not job:
            return json.dumps({"error": f"Job {job_id} not found"})

        cfg = config or Config.from_env()

        # Archive the original Trello card if it exists
        if job.external_id and cfg.trello_api_key and cfg.trello_token:
            auth_params = {"key": cfg.trello_api_key, "token": cfg.trello_token}
            trello_api = "https://api.trello.com/1"
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    await client.put(
                        f"{trello_api}/cards/{job.external_id}",
                        params={**auth_params, "closed": "true"},
                    )
                    logger.info("Archived original Trello card %s", job.external_id)
            except Exception as e:
                logger.warning("Failed to archive Trello card %s: %s", job.external_id, e)

        try:
            if _nats_client:
                await _propose_transition("job", job_id, "done", job_id=job_id)
            else:
                await db.update_job_status(job_id, JobStatus.DONE)
        except InvalidTransitionError as e:
            return json.dumps({"error": str(e), "type": "invalid_transition"})

        await db.record_event(job_id, "phases_created", source="spec_analyst", detail=f"Decomposed into {phase_count} phase cards")

        logger.info("Job %s completed with %d phase cards", job_id, phase_count)
        return json.dumps({"job_id": job_id, "status": "done", "phase_count": phase_count})

    # =========================================================================
    # Log Tools
    # =========================================================================

    @mcp.tool()
    async def get_agent_log(agent_id: str, tail: int = 50) -> str:
        """Get the last N lines of an agent's log file. Useful for debugging agent behavior."""
        agent = await db.get_agent(agent_id)
        if not agent:
            return json.dumps({"error": f"Agent {agent_id} not found"})
        if not agent.log_file:
            return json.dumps({"error": f"No log file for agent {agent_id}"})

        log_path = Path(agent.log_file)
        if not log_path.exists():
            return json.dumps({"error": f"Log file not found: {agent.log_file}"})

        lines = log_path.read_text().splitlines()
        tail_lines = lines[-tail:] if len(lines) > tail else lines
        return json.dumps(
            {
                "agent_id": agent_id,
                "role": agent.role,
                "log_file": agent.log_file,
                "total_lines": len(lines),
                "showing_last": len(tail_lines),
                "lines": tail_lines,
            }
        )

    @mcp.tool()
    async def list_agent_logs(job_id: str) -> str:
        """List all agent log files for a job with their sizes and status."""
        agents = await db.get_agents_for_job(job_id)
        logs = []
        for a in agents:
            entry = {"agent_id": a.id, "role": a.role, "status": a.status, "log_file": a.log_file}
            if a.log_file:
                p = Path(a.log_file)
                entry["exists"] = p.exists()
                if p.exists():
                    entry["size_bytes"] = p.stat().st_size
                    entry["line_count"] = len(p.read_text().splitlines())
            logs.append(entry)
        return json.dumps(logs)

    # =========================================================================
    # Resources
    # =========================================================================

    @mcp.resource("job://{job_id}")
    async def job_resource(job_id: str) -> str:
        """Get full job details including tasks and agents."""
        job = await db.get_job(job_id)
        if not job:
            return json.dumps({"error": "Not found"})
        tasks = await db.get_tasks(job_id)
        agents = await db.get_agents_for_job(job_id)
        return json.dumps(
            {
                "job": job.model_dump(),
                "tasks": [t.model_dump() for t in tasks],
                "agents": [a.model_dump() for a in agents],
            }
        )

    @mcp.resource("job://active")
    async def active_jobs_resource() -> str:
        """List all active (non-terminal) jobs."""
        jobs = await db.get_active_jobs()
        return json.dumps([j.model_dump() for j in jobs])

    @mcp.resource("agents://{job_id}")
    async def agents_resource(job_id: str) -> str:
        """Get all agents for a job."""
        agents = await db.get_agents_for_job(job_id)
        return json.dumps([a.model_dump() for a in agents])

    @mcp.resource("logs://{agent_id}")
    async def agent_log_resource(agent_id: str) -> str:
        """Get the full log content for an agent."""
        agent = await db.get_agent(agent_id)
        if not agent or not agent.log_file:
            return json.dumps({"error": "No log available"})
        log_path = Path(agent.log_file)
        if not log_path.exists():
            return json.dumps({"error": "Log file not found"})
        return log_path.read_text()

    # =========================================================================
    # Memory Tools (gated on memory_enabled)
    # =========================================================================

    if memory_enabled and tuplespace:

        @mcp.tool()
        async def publish_fact(
            project: str,
            category: str,
            key: str,
            value: str,
            tags: list[str] | None = None,
            job_id: str | None = None,
            agent_role: str | None = None,
        ) -> str:
            """Publish a fact to the project's shared tuplespace memory."""
            fact_id = await tuplespace.out(
                category=category,
                key=key,
                value=value,
                tags=tags,
                agent_role=agent_role,
                job_id=job_id,
            )
            return json.dumps({"fact_id": fact_id, "project": project})

        @mcp.tool()
        async def query_facts(
            project: str,
            category: str | None = None,
            key_pattern: str | None = None,
            tags: list[str] | None = None,
            limit: int = 20,
        ) -> str:
            """Query shared facts from the project's tuplespace memory."""
            facts = await tuplespace.rd(category=category, key_pattern=key_pattern, tags=tags, limit=limit)
            return json.dumps([f.model_dump() for f in facts])

        @mcp.tool()
        async def create_memory_note(
            content: str,
            tags: list[str] | None = None,
            project: str | None = None,
            links: list[str] | None = None,
            job_id: str | None = None,
            agent_role: str | None = None,
        ) -> str:
            """Create a persistent memory note. Writes to L2 and queues for L3 archival."""
            fact_id = await tuplespace.out(
                category="memory_note",
                key=f"note-{job_id or 'manual'}",
                value=content,
                tags=tags,
                agent_role=agent_role,
                job_id=job_id,
            )
            return json.dumps({"fact_id": fact_id, "queued_for_archival": True})

    return mcp
