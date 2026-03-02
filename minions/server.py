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

from fastmcp import FastMCP

from .config import Config
from .db import AbstractDatabase
from .models import Job, JobStatus, Message, Review, ReviewStatus, Subtask, SubtaskStatus, Task, TaskStatus, _now
from .state_transitions import InvalidTransitionError, PreconditionError

logger = logging.getLogger(__name__)

# Module-level NATS client reference, set by CLI when arbiter_enabled.
# When set, state-mutating tools route through the Arbiter.
_nats_client = None


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
            entity_type, entity_id,
            response.get("from_status", "?"),
            to_status,
        )
    return response


def create_server(db: AbstractDatabase, config: Config | None = None) -> FastMCP:
    """Create and return the FastMCP server with review + job orchestration tools."""
    mcp = FastMCP("Minion Suite", instructions="AI agent suite — composable, vendor-agnostic agents. Code review + multi-agent job orchestration.")

    # =========================================================================
    # Review Tools (backward compatible)
    # =========================================================================

    @mcp.tool()
    async def request_review(project: str, mr_url: str, mr_id: str) -> str:
        """Queue a new review for a merge/pull request."""
        review = Review(project=project, mr_url=mr_url, mr_id=mr_id)
        review = await db.create_review(review)
        return json.dumps({"review_id": review.id, "status": review.status, "project": project})

    @mcp.tool()
    async def get_review_status(review_id: str) -> str:
        """Get the current status of a review."""
        review = await db.get_review(review_id)
        if not review:
            return json.dumps({"error": f"Review {review_id} not found"})
        return json.dumps({
            "review_id": review.id,
            "project": review.project,
            "mr_url": review.mr_url,
            "status": review.status,
            "verdict": review.verdict,
            "comments_posted": review.comments_posted,
            "error": review.error,
        })

    @mcp.tool()
    async def get_review_history(project: str | None = None, limit: int = 20) -> str:
        """Get recent review history, optionally filtered by project."""
        reviews = await db.get_reviews(project=project, limit=limit)
        return json.dumps([
            {
                "id": r.id,
                "project": r.project,
                "mr_url": r.mr_url,
                "status": r.status,
                "verdict": r.verdict,
                "comments_posted": r.comments_posted,
                "created_at": r.created_at,
                "completed_at": r.completed_at,
            }
            for r in reviews
        ])

    @mcp.tool()
    async def cancel_review(review_id: str) -> str:
        """Cancel a queued review (cannot cancel in-progress reviews)."""
        review = await db.get_review(review_id)
        if not review:
            return json.dumps({"error": f"Review {review_id} not found"})
        if review.status != ReviewStatus.QUEUED:
            return json.dumps({"error": f"Can only cancel queued reviews (current: {review.status})"})
        await db.update_review(review_id, status=ReviewStatus.FAILED, error="Cancelled by user")
        return json.dumps({"review_id": review_id, "status": "cancelled"})

    # =========================================================================
    # Cost & Stats Tools
    # =========================================================================

    @mcp.tool()
    async def get_cost_summary(project: str | None = None, days: int = 30) -> str:
        """Get cost and usage summary for reviews over a time period."""
        summary = await db.get_cost_summary(project=project, days=days)
        return json.dumps(summary)

    @mcp.tool()
    async def get_agent_logs(review_id: str) -> str:
        """List agent invocations for a review with their metrics."""
        agents = await db.get_agents(review_id)
        return json.dumps([
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
        ])

    # =========================================================================
    # Job Management Tools
    # =========================================================================

    @mcp.tool()
    async def submit_spec(spec: str, trello_card_id: str | None = None) -> str:
        """Submit a new feature specification to start a job."""
        job = Job(spec=spec, trello_card_id=trello_card_id)
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
        """Create a development task for a specific service within a job."""
        task = Task(
            job_id=job_id,
            title=title,
            description=description,
            service=service,
            agent_role=agent_role,
        )
        task = await db.create_task(task)
        await db.record_event(job_id, "task_created", "arbiter", f"task={task.id} service={service} role={agent_role}")
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
        return json.dumps({
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
        })

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
                    "task", task_id, "pr_open",
                    job_id=task.job_id,
                    pr_url=pr_url, pr_number=pr_number,
                )
                # Update non-state fields directly (branch_name is not a state transition concern)
                await db.update_task(task_id, branch_name=branch_name)
                task = await db.get_task(task_id)
            else:
                task = await db.update_task(
                    task_id, pr_url=pr_url, pr_number=pr_number, branch_name=branch_name, status=TaskStatus.PR_OPEN
                )
            if not task:
                return json.dumps({"error": f"Task {task_id} not found"})

            if task.job_id:
                await db.record_event(task.job_id, "pr_opened", str(task.agent_role), f"task={task_id} pr={pr_url}")

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
                    "task", task_id, new_status,
                    job_id=task.job_id,
                    review_status=review_status, agent_role="code_reviewer",
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
                await db.record_event(
                    task.job_id, "deploy_status", "deploy_monitor", f"task={task_id} status={status} detail={detail or ''}"
                )

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
                subtask = await db.update_subtask(
                    subtask_id, status=SubtaskStatus.COMPLETED, completed_at=_now(), result=result
                )
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
                subtask = await db.update_subtask(
                    subtask_id, status=SubtaskStatus.FAILED, completed_at=_now(), error=error
                )
            if not subtask:
                return json.dumps({"error": f"Subtask {subtask_id} not found"})
            return json.dumps({"subtask_id": subtask_id, "status": "failed"})
        except InvalidTransitionError as e:
            return json.dumps({"error": str(e)})

    @mcp.tool()
    async def get_subtasks(task_id: str) -> str:
        """Get all subtasks for a task, ordered by sequence number."""
        subtasks = await db.get_subtasks(task_id)
        return json.dumps([
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
        ])

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
        return json.dumps([
            {
                "id": m.id,
                "from_role": m.from_role,
                "to_role": m.to_role,
                "content": m.content,
                "created_at": m.created_at,
            }
            for m in messages
        ])

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
    # Resources
    # =========================================================================

    @mcp.resource("review://{review_id}")
    async def review_resource(review_id: str) -> str:
        """Get full review details including comments."""
        review = await db.get_review(review_id)
        if not review:
            return json.dumps({"error": "Not found"})
        comments = await db.get_comments(review_id)
        agents = await db.get_agents(review_id)
        return json.dumps({
            "review": review.model_dump(),
            "comments": [c.model_dump() for c in comments],
            "agents": [a.model_dump() for a in agents],
        })

    @mcp.resource("job://{job_id}")
    async def job_resource(job_id: str) -> str:
        """Get full job details including tasks and agents."""
        job = await db.get_job(job_id)
        if not job:
            return json.dumps({"error": "Not found"})
        tasks = await db.get_tasks(job_id)
        agents = await db.get_agents_for_job(job_id)
        return json.dumps({
            "job": job.model_dump(),
            "tasks": [t.model_dump() for t in tasks],
            "agents": [a.model_dump() for a in agents],
        })

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

    return mcp
