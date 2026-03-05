"""Development job handlers — standalone functions receiving the engine instance."""

import json
import logging
from typing import TYPE_CHECKING

from ..agents.prompt import build_agent_prompt
from ..agents.runner import run_agent
from ..core.models import Agent, AgentRole, Job, JobStatus, Task, TaskStatus
from ..core.state_transitions import InvalidTransitionError

if TYPE_CHECKING:
    from .job_engine import JobEngine

logger = logging.getLogger(__name__)


async def launch_spec_analyst(engine: "JobEngine", job: Job):
    """Launch the spec analyst agent to refine the raw spec."""
    if await engine._has_running_agent(job.id, AgentRole.SPEC_ANALYST):
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
    task = await engine.db.create_task(task)

    agent = Agent(job_id=job.id, role=AgentRole.SPEC_ANALYST, task_id=task.id, model=engine.config.model)
    agent = await engine.db.create_agent(agent)

    logger.info("Launching spec analyst for job %s", job.id)
    await engine.db.record_event(job.id, "agent_launched", "engine", f"agent={agent.id} role=spec_analyst")
    await engine._nats_agent_status(job.id, agent.id, "spec_analyst", "launched")
    await engine._trello_comment(job, f"Spec analyst started (agent={agent.id[:8]})")

    if engine._k8s_enabled:
        prompt = engine._maybe_dry_run(build_agent_prompt(job, task, None, None))
        await engine._dispatch_k8s(job, agent, AgentRole.SPEC_ANALYST, prompt, engine._default_working_dir())
        return

    result_agent = await engine._run_in_process(job, task, agent, None, None)

    if result_agent.status == "done":
        # If arbiter not enabled, check if spec analyst advanced the job
        if not engine.config.arbiter_enabled:
            updated_job = await engine.db.get_job(job.id)
            if updated_job and updated_job.status == JobStatus.SPEC_RECEIVED:
                logger.warning("Spec analyst completed but didn't submit refined spec for job %s, advancing with raw spec", job.id)
                await engine.db.update_job_status(job.id, JobStatus.SPEC_READY)
    else:
        await engine.db.update_job_status(job.id, JobStatus.FAILED, error=f"Spec analyst failed: {result_agent.error or 'unknown'}")
        await engine._on_job_terminal(job.id)


async def launch_arbiter(engine: "JobEngine", job: Job):
    """Launch the arbiter agent to break down the spec into tasks."""
    if await engine._has_running_agent(job.id, AgentRole.ARBITER):
        return

    # Re-fetch job to get the refined spec
    job = await engine.db.get_job(job.id) or job

    task = Task(
        job_id=job.id,
        title="Create task plan",
        description=job.spec,
        service="_arbiter",
        agent_role=AgentRole.ARBITER,
        status=TaskStatus.IN_PROGRESS,
    )
    task = await engine.db.create_task(task)

    agent = Agent(job_id=job.id, role=AgentRole.ARBITER, task_id=task.id, model=engine.config.model)
    agent = await engine.db.create_agent(agent)

    logger.info("Launching arbiter for job %s", job.id)
    await engine.db.record_event(job.id, "agent_launched", "engine", f"agent={agent.id} role=arbiter")
    await engine._nats_agent_status(job.id, agent.id, "arbiter", "launched")
    await engine._trello_comment(job, f"Arbiter started (agent={agent.id[:8]})")

    if engine._k8s_enabled:
        prompt = engine._maybe_dry_run(build_agent_prompt(job, task, None, None))
        await engine._dispatch_k8s(job, agent, AgentRole.ARBITER, prompt, engine._default_working_dir())
        return

    result_agent = await engine._run_in_process(job, task, agent, None, None)

    if result_agent.status == "done":
        if not engine.config.arbiter_enabled:
            updated_job = await engine.db.get_job(job.id)
            if updated_job and updated_job.status == JobStatus.SPEC_READY:
                tasks = await engine.db.get_tasks(job.id)
                # Filter out the virtual arbiter task itself
                real_tasks = [t for t in tasks if t.service not in ("_spec", "_arbiter")]
                if real_tasks:
                    await engine.db.update_job_status(job.id, JobStatus.TASKS_CREATED)
                else:
                    await engine.db.update_job_status(job.id, JobStatus.FAILED, error="Arbiter created no tasks")
                    await engine._on_job_terminal(job.id)
    else:
        await engine.db.update_job_status(job.id, JobStatus.FAILED, error=f"Arbiter failed: {result_agent.error or 'unknown'}")
        await engine._on_job_terminal(job.id)


async def launch_engineers(engine: "JobEngine", job: Job):
    """Launch engineering agents in parallel for pending tasks."""
    engineer_roles = {AgentRole.BACKEND_ENGINEER, AgentRole.FRONTEND_ENGINEER, AgentRole.DATABASE_ENGINEER}

    pending_tasks = await engine.db.get_tasks_by_status(job.id, TaskStatus.PENDING)
    pending_tasks = [t for t in pending_tasks if t.agent_role in engineer_roles]

    fresh_tasks = [t for t in pending_tasks if t.attempt <= 1]
    retry_tasks = [t for t in pending_tasks if t.attempt > 1]

    # Also pick up tasks needing revisions
    in_progress_tasks = await engine.db.get_tasks_by_status(job.id, TaskStatus.IN_PROGRESS)
    revision_tasks = [t for t in in_progress_tasks if t.review_status == "changes_requested" and t.agent_role in engineer_roles]

    all_tasks = fresh_tasks + retry_tasks + revision_tasks
    if not all_tasks:
        all_job_tasks = await engine.db.get_tasks(job.id)
        real_tasks = [t for t in all_job_tasks if t.service not in ("_spec", "_arbiter")]
        if not real_tasks:
            logger.info("Job %s has no tasks -- arbiter determined no work needed", job.id)
            await engine.db.update_job_status(job.id, JobStatus.NO_WORK_NEEDED)
            await engine._trello_comment(job, "No tasks needed -- arbiter determined no changes required.")
            return
        return

    await engine.db.update_job_status(job.id, JobStatus.DEV_IN_PROGRESS)

    for t in fresh_tasks:
        engine._spawn(run_engineer(engine, job, t, is_revision=False), name=f"eng-{t.id[:8]}")
    for t in retry_tasks:
        engine._spawn(run_engineer(engine, job, t, is_retry=True), name=f"eng-retry-{t.id[:8]}")
    for t in revision_tasks:
        engine._spawn(run_engineer(engine, job, t, is_revision=True), name=f"eng-rev-{t.id[:8]}")


async def run_engineer(engine: "JobEngine", job: Job, task: Task, is_revision: bool = False, is_retry: bool = False):
    """Run a single engineering agent for a task."""
    project, service = engine._resolve_service(task.service)
    if not service:
        logger.error("Unknown service %s for task %s", task.service, task.id)
        await engine.db.update_task(task.id, status=TaskStatus.FAILED, error=f"Unknown service: {task.service}")
        return

    context = None

    if is_retry:
        checkpoint_summary = await build_checkpoint_summary(engine, task.id)
        prior_error = task.error or "Unknown error"
        branch_name = task.branch_name
        if not branch_name:
            short_job_id = job.id[:8]
            slug = task.title.lower().replace(" ", "-")[:30]
            branch_name = f"feat/job-{short_job_id}/{slug}"

        try:
            await engine.db.update_task(task.id, branch_name=branch_name, status=TaskStatus.IN_PROGRESS)
        except InvalidTransitionError as e:
            logger.warning("Rejected task transition for %s: %s", task.id, e)
            return

        context = f"## Retry Context (attempt {task.attempt}/{task.max_attempts})\n\nPrior error: {prior_error}\n\n{checkpoint_summary}"

    elif is_revision:
        branch_name = task.branch_name
        try:
            await engine.db.update_task(task.id, review_status="revision_in_progress")
        except InvalidTransitionError as e:
            logger.warning("Rejected task transition for %s: %s", task.id, e)
            return

        review_feedback = await get_review_feedback(engine, job.id, task)
        context = f"## Revision Context (revision {task.revision_count})\n\n{review_feedback}"

    else:
        # Fresh task — generate branch name and transition to in_progress
        short_job_id = job.id[:8]
        slug = task.title.lower().replace(" ", "-")[:30]
        branch_name = f"feat/job-{short_job_id}/{slug}"
        try:
            await engine.db.update_task(task.id, branch_name=branch_name, status=TaskStatus.IN_PROGRESS)
        except InvalidTransitionError as e:
            logger.warning("Rejected task transition for %s: %s", task.id, e)
            return

    agent = Agent(job_id=job.id, role=task.agent_role, task_id=task.id, model=engine.config.model)
    agent = await engine.db.create_agent(agent)

    action = "retry" if is_retry else ("revision" if is_revision else "development")
    event_detail = f"agent={agent.id} role={task.agent_role} task={task.id} action={action}"
    await engine.db.record_event(job.id, "agent_launched", "engine", event_detail)
    await engine._nats_agent_status(job.id, agent.id, str(task.agent_role), "launched")
    await engine._trello_comment(job, f"{task.agent_role} started {action} on {task.service} (agent={agent.id[:8]})")

    if engine._k8s_enabled:
        prompt = engine._maybe_dry_run(build_agent_prompt(job, task, project, service, context))
        await engine._dispatch_k8s(job, agent, str(task.agent_role), prompt, service.repo_path, service=service)
        return

    result_agent = await engine._run_in_process(job, task, agent, project, service, context)

    if result_agent.status == "done":
        # After a successful revision, ensure the task moves back to PR_OPEN for re-review
        if is_revision:
            current_task = await engine.db.get_task(task.id)
            if current_task and current_task.status not in (TaskStatus.PR_OPEN, TaskStatus.MERGED, TaskStatus.DONE):
                try:
                    await engine.db.update_task(task.id, status=TaskStatus.PR_OPEN, review_status="revision_complete")
                    logger.info("Revision agent completed, task %s -> PR_OPEN for re-review", task.id)
                except InvalidTransitionError as e:
                    logger.warning("Rejected task transition for %s: %s", task.id, e)
    else:
        try:
            await engine.db.update_task(task.id, status=TaskStatus.FAILED, error=(result_agent.error or "unknown")[:200])
        except InvalidTransitionError:
            logger.warning("Could not mark task %s as failed", task.id)


async def get_review_feedback(engine: "JobEngine", job_id: str, task: Task) -> str:
    """Fetch review feedback messages for a task from the code_reviewer."""
    messages = await engine.db.get_messages(job_id)
    feedback_parts = []
    for msg in messages:
        if msg.from_role == AgentRole.CODE_REVIEWER and msg.to_role == task.agent_role:
            feedback_parts.append(msg.content)
    if feedback_parts:
        return "\n\n---\n\n".join(feedback_parts)
    return "The reviewer requested changes but no specific feedback was found in messages."


async def run_task_review(engine: "JobEngine", job: Job, task: Task):
    """Launch a code reviewer for a single task's PR."""
    from .review import _create_provider_for_project

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
    reviewer_task = await engine.db.create_task(reviewer_task)

    agent = Agent(job_id=job.id, role=AgentRole.CODE_REVIEWER, task_id=reviewer_task.id, model=engine.config.model)
    agent = await engine.db.create_agent(agent)

    project, service = engine._resolve_service(task.service)

    await engine.db.record_event(job.id, "agent_launched", "engine", f"agent={agent.id} role=code_reviewer task={task.id}")
    await engine._nats_agent_status(job.id, agent.id, "code_reviewer", "launched")
    await engine._trello_comment(job, f"Code reviewer started for {task.service} (agent={agent.id[:8]})")

    context = f"## Review Target\n\n{review_context}"

    if engine._k8s_enabled:
        prompt = engine._maybe_dry_run(build_agent_prompt(job, reviewer_task, project, service, context))
        working_dir = service.repo_path if service else "."
        await engine._dispatch_k8s(job, agent, AgentRole.CODE_REVIEWER, prompt, working_dir, service=service)
        return

    # Create git provider and MR info so the reviewer gets a working ToolExecutor
    provider = None
    mr_info = {}
    if project and task.pr_url:
        try:
            provider = _create_provider_for_project(project, engine.config)
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
        config=engine.config,
        db=engine.db,
        provider=provider,
        mr_info=mr_info if provider else None,
        context=context,
    )

    if result_agent.status != "done":
        # Reset original task from in_review back to pr_open for retry
        try:
            await engine.db.update_task(task.id, status=TaskStatus.PR_OPEN)
            logger.info("Reviewer failed, task %s reset to PR_OPEN for retry", task.id)
        except InvalidTransitionError:
            logger.warning("Could not reset task %s to PR_OPEN after reviewer failure", task.id)


async def manage_dev_tasks(engine: "JobEngine", job: Job):
    """Per-task lifecycle manager: review launches, revision cycles, job advancement."""
    engineer_roles = {AgentRole.BACKEND_ENGINEER, AgentRole.FRONTEND_ENGINEER, AgentRole.DATABASE_ENGINEER}
    tasks = await engine.db.get_tasks(job.id)
    dev_tasks = [t for t in tasks if t.agent_role in engineer_roles]

    if not dev_tasks:
        return

    terminal_statuses = {TaskStatus.MERGED, TaskStatus.DONE, TaskStatus.FAILED}

    for task in dev_tasks:
        if task.status == TaskStatus.PR_OPEN:
            # PR just opened — transition to in_review and spawn reviewer
            try:
                await engine.db.update_task(task.id, status=TaskStatus.IN_REVIEW)
            except InvalidTransitionError as e:
                logger.warning("Could not transition task %s to in_review: %s", task.id, e)
                continue
            engine._spawn(run_task_review(engine, job, task), name=f"review-{task.id[:8]}")

        elif task.status == TaskStatus.PENDING:
            # Task recovered by startup cleanup or arbiter retry — re-launch
            is_retry = task.attempt > 1
            engine._spawn(
                run_engineer(engine, job, task, is_retry=is_retry),
                name=f"eng-{'retry' if is_retry else 'recover'}-{task.id[:8]}",
            )

        elif task.status == TaskStatus.IN_PROGRESS and task.review_status == "changes_requested":
            # Reviewer requested changes — launch revision engineer
            if task.revision_count >= engine.config.max_revisions:
                logger.warning("Task %s hit max revisions (%d), failing", task.id, engine.config.max_revisions)
                try:
                    await engine.db.update_task(
                        task.id, status=TaskStatus.FAILED, error=f"Max revisions ({engine.config.max_revisions}) exceeded"
                    )
                except InvalidTransitionError:
                    pass
                await engine.db.record_event(
                    job.id, "task_max_revisions", "engine", f"task={task.id} revision_count={task.revision_count}"
                )
                continue
            await engine.db.update_task(task.id, revision_count=task.revision_count + 1)
            await engine.db.record_event(
                job.id,
                "task_revision_requested",
                "engine",
                f"task={task.id} revision={task.revision_count + 1}/{engine.config.max_revisions}",
            )
            engine._spawn(run_engineer(engine, job, task, is_revision=True), name=f"eng-rev-{task.id[:8]}")

        elif task.status in (TaskStatus.IN_PROGRESS, TaskStatus.IN_REVIEW):
            # Check if the agent is actually dead (orphaned task)
            latest_agent = await engine.db.get_agent_for_task(task.id)
            if latest_agent and latest_agent.status in ("failed", "completed"):
                if task.status == TaskStatus.IN_REVIEW:
                    try:
                        await engine.db.update_task(task.id, status=TaskStatus.PR_OPEN)
                        logger.info("Orphan recovery: task %s reset from in_review to pr_open", task.id)
                    except InvalidTransitionError as e:
                        logger.warning("Orphan recovery: could not reset task %s to pr_open: %s", task.id, e)
                else:
                    if task.attempt < task.max_attempts:
                        try:
                            await engine.db.update_task(task.id, status=TaskStatus.FAILED, error="agent died without completing")
                            await engine.db.update_task(task.id, status=TaskStatus.PENDING, attempt=task.attempt + 1)
                            logger.info("Orphan recovery: task %s reset to pending (attempt %d)", task.id, task.attempt + 1)
                        except InvalidTransitionError as e:
                            logger.warning("Orphan recovery: could not reset task %s: %s", task.id, e)
                    else:
                        try:
                            await engine.db.update_task(task.id, status=TaskStatus.FAILED, error="max attempts reached after agent death")
                        except InvalidTransitionError:
                            pass

        elif task.status == TaskStatus.FAILED and task.attempt < task.max_attempts:
            # Failed task with retries remaining — auto-retry
            try:
                await engine.db.update_task(task.id, status=TaskStatus.PENDING, attempt=task.attempt + 1, error=None)
                logger.info("Auto-retry: task %s reset to pending (attempt %d/%d)", task.id, task.attempt + 1, task.max_attempts)
                await engine.db.record_event(
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
        await engine.db.update_job_status(job.id, JobStatus.FAILED, error="All dev tasks failed")
        await engine._on_job_terminal(job.id)
        return

    has_merged = any(t.status in (TaskStatus.MERGED, TaskStatus.DONE) for t in dev_tasks)
    if has_merged:
        await engine.db.update_job_status(job.id, JobStatus.MERGED)
        logger.info("Job %s: all dev tasks terminal, advancing to MERGED", job.id)
    else:
        await engine.db.update_job_status(job.id, JobStatus.FAILED, error="All dev tasks terminal but none merged")
        await engine._on_job_terminal(job.id)


async def build_checkpoint_summary(engine: "JobEngine", task_id: str) -> str:
    """Assemble a checkpoint summary from persisted subtask results for retry agents."""
    task = await engine.db.get_task(task_id)
    if not task:
        return "No task data available."

    parts = []

    if task.branch_name:
        parts.append(f"Branch: `{task.branch_name}`")
    if task.pr_url:
        parts.append(f"Existing PR: {task.pr_url}")

    agents = await engine.db.get_agents_for_job(task.job_id)
    task_agents = [a for a in agents if a.task_id == task_id]
    if task_agents:
        parts.append(f"Prior attempts: {len(task_agents)}")
        for a in task_agents:
            status_line = f"  - Agent {a.id[:8]}: status={a.status}, cost=${a.cost_usd:.4f}"
            if a.error:
                status_line += f", error={a.error[:150]}"
            parts.append(status_line)

    subtasks = await engine.db.get_subtasks(task_id)
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
