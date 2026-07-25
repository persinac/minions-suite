"""Review job handlers — standalone functions receiving the engine instance."""

import logging
from typing import TYPE_CHECKING

from ..agents.runner import run_agent
from ..core.models import Agent, AgentRole, Job, JobStatus, Task, TaskStatus
from ..core.state_transitions import InvalidTransitionError
from ..project_registry import ProjectConfig

if TYPE_CHECKING:
    from .job_engine import JobEngine

logger = logging.getLogger(__name__)


async def launch_review_tasks(engine: JobEngine, job: Job):
    """Launch CODE_REVIEWER tasks for a review-type job."""
    pending_tasks = await engine.db.get_tasks_by_status(job.id, TaskStatus.PENDING)
    review_tasks = [t for t in pending_tasks if t.agent_role == AgentRole.CODE_REVIEWER]

    if not review_tasks:
        logger.warning("Review job %s has no pending reviewer tasks", job.id)
        await engine.db.update_job_status(job.id, JobStatus.FAILED, error="No reviewer tasks found")
        return

    await engine.db.update_job_status(job.id, JobStatus.REVIEW_IN_PROGRESS)

    for task in review_tasks:
        # Claim task before spawning to prevent duplicate agents on next poll
        try:
            await engine.db.update_task(task.id, status=TaskStatus.IN_PROGRESS, agent_role="")
        except InvalidTransitionError as e:
            logger.warning("Could not claim review task %s: %s", task.id, e)
            continue
        engine._spawn(run_review_in_process(engine, job, task), name=f"review-{task.id[:8]}")


async def run_review_in_process(engine: JobEngine, job: Job, task: Task):
    """Run a single review task in-process using the unified agent loop."""

    # Resolve project config
    project = engine.registry.get(task.service)
    if not project:
        provider_hint = "gitlab"
        if task.mr_url and "/pull/" in task.mr_url:
            provider_hint = "github"
        project = ProjectConfig(
            name=task.service,
            project_id="",
            git_provider=provider_hint,
            gitlab_url=engine.config.gitlab_url,
            model=engine.config.model,
        )

    # Create git provider
    try:
        provider = await create_reviewer_provider(project, engine.config)
    except ValueError as e:
        logger.error("Failed to create provider for task %s: %s", task.id, e)
        await engine.db.update_task(task.id, status=TaskStatus.FAILED, agent_role="", error=str(e)[:200])
        return

    # Task already claimed as IN_PROGRESS by launch_review_tasks()

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

    # Create agent record — use project model if available
    model = project.model if project and project.model else engine.config.model
    agent = Agent(job_id=job.id, role=AgentRole.CODE_REVIEWER, task_id=task.id, model=model)
    agent = await engine.db.create_agent(agent)

    await engine.db.record_event(job.id, "agent_launched", "engine", f"agent={agent.id} role=code_reviewer task={task.id}")
    await engine._nats_agent_status(job.id, agent.id, "code_reviewer", "launched")

    # Build file knowledge context from memory system (when enabled)
    knowledge_ctx = None
    if engine.memory_store and engine.config.memory_enabled:
        try:
            from agent_memory.context import build_file_context

            changed_files = mr_info.get("changed_files", [])
            knowledge_ctx = await build_file_context(
                engine.memory_store, task.service, changed_files, max_tokens=engine.config.memory_l3_token_budget
            )
        except Exception as e:
            logger.warning("Failed to build file context for review: %s", e)

    # Run the agent
    result_agent = await run_agent(
        job=job,
        task=task,
        project=project,
        config=engine.config,
        db=engine.db,
        provider=provider,
        mr_info=mr_info,
        agent=agent,
        knowledge_context=knowledge_ctx,
    )

    # Update task with review results
    if result_agent.status == "done":
        verdict = getattr(result_agent, "_review_verdict", None)
        comments_posted = getattr(result_agent, "_review_comments_posted", 0)
        try:
            await engine.db.update_task(
                task.id,
                status=TaskStatus.DONE,
                agent_role="",
                verdict=verdict,
                comments_posted=comments_posted,
            )
        except InvalidTransitionError as e:
            logger.warning("Could not mark review task %s as done: %s", task.id, e)

        await engine.db.record_event(
            job.id,
            "review_complete",
            "engine",
            f"task={task.id} verdict={verdict} comments={comments_posted}",
        )
        await engine._nats_agent_status(job.id, agent.id, "code_reviewer", "completed")
    else:
        error = result_agent.error or "unknown"
        try:
            await engine.db.update_task(task.id, status=TaskStatus.FAILED, agent_role="", error=error[:200])
        except InvalidTransitionError:
            logger.warning("Could not mark review task %s as failed", task.id)
        await engine._nats_agent_status(job.id, agent.id, "code_reviewer", "failed")


async def check_review_tasks(engine: JobEngine, job: Job):
    """Check if all review tasks are terminal and advance the job."""
    tasks = await engine.db.get_tasks(job.id)
    review_tasks = [t for t in tasks if t.agent_role == AgentRole.CODE_REVIEWER]

    if not review_tasks:
        return

    terminal = {TaskStatus.DONE, TaskStatus.FAILED}
    all_terminal = all(t.status in terminal for t in review_tasks)
    if not all_terminal:
        return

    all_failed = all(t.status == TaskStatus.FAILED for t in review_tasks)
    if all_failed:
        await engine.db.update_job_status(job.id, JobStatus.FAILED, error="All review tasks failed")
    else:
        await engine.db.update_job_status(job.id, JobStatus.DONE)
        logger.info("Review job %s completed", job.id)


async def create_engineer_provider(project, config):
    """Git provider carrying the *engineer* identity — the one that can write.

    Merging is a write to the base branch, and `--delete-branch` removes a ref,
    so it needs Contents: Read & write. The reviewer App deliberately has
    read-only Contents: its whole job is to approve, and giving it write to every
    repo purely so it can press a button defeats the point of splitting the
    identities. GitHub never blocks an identity from merging a PR it opened — it
    only blocks it from *approving* one — so the engineer is the right principal
    here and needs no extra grant.

    Uses the App installation token explicitly rather than relying on the ambient
    GH_TOKEN that ensure_token happens to leave in os.environ: config.github_token
    is empty in this deployment, so an implicit path would work only by accident
    and would break silently the moment the process environment changed.
    """
    from ..providers.git import create_provider
    from ..providers.github_app import ensure_token

    provider_type = project.git_provider or config.git_provider
    if provider_type != "github":
        return _create_provider_for_project(project, config)

    token = await ensure_token(config)
    if not token:
        return _create_provider_for_project(project, config)

    return create_provider("github", token=token)


async def create_reviewer_provider(project, config):
    """Git provider for the code reviewer, using the reviewer App if configured.

    GitHub refuses a formal review from the identity that opened the pull
    request, and the engineer App opens every minion PR. Handing the reviewer a
    token from a *second* App is what makes an actual APPROVED /
    CHANGES_REQUESTED possible rather than a comment.

    Falls back to the normal provider when no reviewer App is configured, in
    which case GitHubProvider.submit_review degrades to a PR comment.

    Only the review path uses this. Everything else keeps the engineer identity,
    which is correct — clones, commits and pushes must stay attributable to the
    App that owns the branch.
    """
    from ..providers.git import create_provider
    from ..providers.github_app import reviewer_token

    provider_type = project.git_provider or config.git_provider
    if provider_type != "github":
        return _create_provider_for_project(project, config)

    token = await reviewer_token(config)
    if not token:
        return _create_provider_for_project(project, config)

    logger.info("Using the reviewer GitHub App identity for %s", project.project_id)
    return create_provider("github", token=token)


def _create_provider_for_project(project, config):
    """Create the appropriate git provider for a project."""
    from ..providers.git import create_provider

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
