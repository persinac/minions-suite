"""Deploy job handlers — standalone functions receiving the engine instance."""

import json
import logging
from typing import TYPE_CHECKING

from ..agents.prompt import build_agent_prompt
from ..classifier import resolve_model
from ..core.models import Agent, AgentRole, Job, JobStatus, Task, TaskStatus
from ..core.state_transitions import InvalidTransitionError

if TYPE_CHECKING:
    from .job_engine import JobEngine

logger = logging.getLogger(__name__)


async def launch_deploy_monitor(engine: JobEngine, job: Job):
    """Launch the deploy monitor agent."""
    if await engine._has_running_agent(job.id, AgentRole.DEPLOY_MONITOR):
        return

    tasks = await engine.db.get_tasks(job.id)
    merged_tasks = [t for t in tasks if t.status == TaskStatus.MERGED]

    # Auto-complete tasks with no deploy target
    for t in list(merged_tasks):
        _, service = engine._resolve_service(t.service)
        if service and service.deploy_target == "none":
            logger.info("Task %s (%s) has no deploy target, marking done", t.id, t.service)
            await engine.db.update_task(t.id, status=TaskStatus.DONE, agent_role="")
            merged_tasks.remove(t)

    if not merged_tasks:
        engineer_roles = {AgentRole.BACKEND_ENGINEER, AgentRole.FRONTEND_ENGINEER, AgentRole.DATABASE_ENGINEER}
        dev_tasks = [t for t in tasks if t.agent_role in engineer_roles]
        all_done = dev_tasks and all(t.status in (TaskStatus.DONE, TaskStatus.FAILED) for t in dev_tasks)
        if all_done:
            logger.info("Job %s: all tasks already done, skipping deploy monitor", job.id)
            await engine.db.update_job_status(job.id, JobStatus.DEPLOYED)
        return

    await engine.db.update_job_status(job.id, JobStatus.DEPLOYING)

    for t in merged_tasks:
        await engine.db.update_task(t.id, status=TaskStatus.DEPLOYING, agent_role="")

    deploy_task = Task(
        job_id=job.id,
        title="Monitor deployments",
        description=f"Monitor CI/CD for: {', '.join(sorted(set(t.service for t in merged_tasks)))}",
        service="_deploy",
        agent_role=AgentRole.DEPLOY_MONITOR,
        status=TaskStatus.IN_PROGRESS,
    )
    deploy_task = await engine.db.create_task(deploy_task)

    agent = Agent(job_id=job.id, role=AgentRole.DEPLOY_MONITOR, task_id=deploy_task.id, model=resolve_model(engine.config, job.difficulty))
    agent = await engine.db.create_agent(agent)

    deploy_info = json.dumps([{"task_id": t.id, "title": t.title, "service": t.service, "branch": t.branch_name} for t in merged_tasks], indent=2)
    context = f"## Deploy Targets\n\n{deploy_info}"

    await engine.db.record_event(job.id, "agent_launched", "engine", f"agent={agent.id} role=deploy_monitor task={deploy_task.id}")
    await engine._nats_agent_status(job.id, agent.id, "deploy_monitor", "launched")
    await engine._trello_comment(job, f"Deploy monitor started (agent={agent.id[:8]})")

    if engine._k8s_enabled:
        prompt = engine._maybe_dry_run(build_agent_prompt(job, deploy_task, None, None, context))
        await engine._dispatch_k8s(job, agent, AgentRole.DEPLOY_MONITOR, prompt, engine._default_working_dir())
        return

    result_agent = await engine._run_in_process(job, deploy_task, agent, None, None, context)

    if result_agent.status == "done":
        try:
            await engine.db.update_task(deploy_task.id, status=TaskStatus.DONE, agent_role="")
        except InvalidTransitionError:
            logger.warning("Could not mark deploy task %s as done", deploy_task.id)
    else:
        try:
            await engine.db.update_task(deploy_task.id, status=TaskStatus.FAILED, agent_role="", error=(result_agent.error or "unknown")[:200])
        except InvalidTransitionError:
            logger.warning("Could not mark deploy task %s as failed", deploy_task.id)


async def check_deployed(engine: JobEngine, job: Job):
    """Check if all deployments are complete."""
    engineer_roles = {AgentRole.BACKEND_ENGINEER, AgentRole.FRONTEND_ENGINEER, AgentRole.DATABASE_ENGINEER}
    tasks = await engine.db.get_tasks(job.id)
    dev_tasks = [t for t in tasks if t.agent_role in engineer_roles]
    deploy_tasks = [t for t in dev_tasks if t.status in (TaskStatus.DEPLOYING, TaskStatus.DONE, TaskStatus.FAILED)]

    if not deploy_tasks:
        return

    all_done = all(t.status in (TaskStatus.DONE, TaskStatus.FAILED) for t in deploy_tasks)
    if all_done:
        any_failed = any(t.status == TaskStatus.FAILED for t in deploy_tasks)
        if any_failed:
            await engine.db.update_job_status(job.id, JobStatus.FAILED, error="One or more deployments failed")
            await engine._on_job_terminal(job.id)
        else:
            await engine.db.update_job_status(job.id, JobStatus.DEPLOYED)
            await engine._on_job_terminal(job.id)
