"""Development job handlers — standalone functions receiving the engine instance."""

import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ..agents.prompt import build_agent_prompt
from ..agents.runner import run_agent
from ..classifier import classify_difficulty, resolve_model
from ..core.models import Agent, AgentRole, Job, JobStatus, Task, TaskStatus
from ..core.state_transitions import InvalidTransitionError, PreconditionError

if TYPE_CHECKING:
    from .job_engine import JobEngine

logger = logging.getLogger(__name__)

SUBTASK_TERMINAL = {"completed", "failed"}


async def _all_subtasks_terminal(engine: JobEngine, task_id: str) -> bool:
    """Return True if the task has subtasks and all are in a terminal state."""
    subtasks = await engine.db.get_subtasks(task_id)
    if not subtasks:
        return True  # No subtasks — nothing to gate on
    return all(s.status in SUBTASK_TERMINAL for s in subtasks)


# Upper bound on how long a stale-looking agent may defer orphan recovery.
# Past this, the agent is judged regardless of ordering — deferring forever
# would turn a slow recovery into a silent hang, which is the worse failure.
ORPHAN_GRACE_SECONDS = 60


def _parse_ts(value) -> datetime | None:
    """Parse a timestamp that may already be a datetime or an ISO string."""
    if not value:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value


def _agent_predates_current_attempt(agent, task: Task) -> bool:
    """True if `agent` is a leftover from an attempt before the task's current one.

    Claiming a task sets it IN_PROGRESS and *then* spawns run_engineer, which
    creates the new agent row — so for a moment the newest agent on record
    started BEFORE the task was last modified. Recovering against it consumes an
    attempt for work that is already being redone: on job 095146b8 that ate
    attempt 3 six seconds after attempt 2, and the task failed with
    max_attempts exhausted having only ever run a single agent.

    An agent that started at or after the task's last update belongs to this
    attempt and is judged normally, so a genuinely orphaned task still recovers
    immediately. Unreadable timestamps also fall through to judging. And the
    deferral is bounded by ORPHAN_GRACE_SECONDS, so a task whose updated_at
    moved for some unrelated reason cannot defer recovery forever.
    """
    agent_started = _parse_ts(getattr(agent, "started_at", None))
    task_updated = _parse_ts(getattr(task, "updated_at", None))
    if not agent_started or not task_updated:
        return False
    if agent_started >= task_updated:
        return False
    return (datetime.now(UTC) - task_updated).total_seconds() < ORPHAN_GRACE_SECONDS


async def _label_minions_mr(engine: JobEngine, task: Task) -> None:
    """Add a minions-job-<id> label to the MR so webhooks can skip it."""
    import re

    from ..providers.git import create_provider

    mr_url = task.pr_url or task.mr_url
    if not mr_url:
        return

    mr_id = task.mr_id or str(task.pr_number or "")
    if not mr_id:
        match = re.search(r"/merge_requests/(\d+)", mr_url)
        if not match:
            match = re.search(r"/pull/(\d+)", mr_url)
        if match:
            mr_id = match.group(1)
    if not mr_id:
        return

    project = engine.registry.get(task.service)
    if not project:
        return

    try:
        provider_type = project.git_provider or engine.config.git_provider
        if provider_type == "gitlab":
            provider = create_provider("gitlab", gitlab_url=project.gitlab_url or engine.config.gitlab_url, token=engine.config.gitlab_token)
        elif provider_type == "github":
            provider = create_provider("github", token=engine.config.github_token)
        else:
            return

        labels = [f"minions-job-{task.job_id[:8]}"]
        result = await provider.add_mr_labels(project.project_id, mr_id, labels)
        if result.get("labels_added"):
            logger.info("Labeled MR %s with %s", mr_url, labels)
        else:
            logger.warning("Failed to label MR %s: %s", mr_url, result.get("error", "unknown"))
    except Exception as e:
        logger.warning("Could not label MR %s: %s", mr_url, e)


async def _try_complete_task(engine: JobEngine, task: Task, label: str) -> None:
    """Advance a task after its agent finishes successfully.

    Rules:
    1. All subtasks must be in a terminal state (completed/failed).
    2. Engineer tasks must have a PR (pr_url set) — go to PR_OPEN for review.
    3. If either condition is unmet and retries remain, retry the task.

    Called when an agent finishes successfully but the task is still in_progress.
    """
    engineer_roles = {AgentRole.BACKEND_ENGINEER, AgentRole.FRONTEND_ENGINEER}
    subtasks_done = await _all_subtasks_terminal(engine, task.id)

    # Re-read task to get latest pr_url (agent may have set it during execution)
    current = await engine.db.get_task(task.id)
    if current:
        task = current

    # Single-owner guard. TWO paths land here for the same finished agent:
    # run_engineer's own completion handler, and the poll loop's IN_PROGRESS
    # orphan recovery, which sees `latest_agent.status == "done"` and concludes
    # nobody updated the task. They fire ~1s apart, and each one that reaches
    # the retry branch below increments `attempt` off its own stale read.
    #
    # On job 095146b8 that burned all three attempts in TWELVE SECONDS
    # (attempt 2 logged twice, then 3, then FAILED) without ever launching a
    # second agent -- so max_attempts=3 behaved as max_attempts=1 and a
    # cost-limited engineer got no real retry.
    #
    # Whoever wins moves the task out of IN_PROGRESS; the loser sees that here
    # and returns. This is the retry-path instance of the same ambiguity that
    # wedged revisions: "nobody handled this" is indistinguishable from
    # "someone else is handling it right now" unless one of them claims it.
    if task.status != TaskStatus.IN_PROGRESS:
        logger.debug("%s: task %s is already %s — completion handled elsewhere", label, task.id, task.status)
        return

    needs_pr = task.agent_role in engineer_roles
    has_pr = bool(task.pr_url)

    if subtasks_done and needs_pr and has_pr:
        # Engineer with a PR — move to PR_OPEN for code review
        try:
            await engine.db.update_task(task.id, status=TaskStatus.PR_OPEN, agent_role="")
            logger.info("%s: task %s -> PR_OPEN (ready for review)", label, task.id)
        except InvalidTransitionError as e:
            logger.warning("%s: rejected task transition for %s: %s", label, task.id, e)
            return

        # Label the MR so webhooks can ignore minions-created MRs
        await _label_minions_mr(engine, task)
    elif subtasks_done and not needs_pr:
        # Non-engineer (database_engineer, etc.) — mark done directly
        try:
            await engine.db.update_task(task.id, status=TaskStatus.DONE, agent_role="")
            logger.info("%s: task %s -> DONE (all subtasks terminal)", label, task.id)
        except InvalidTransitionError as e:
            logger.warning("%s: rejected task transition for %s: %s", label, task.id, e)
    elif subtasks_done and needs_pr and not has_pr and await _spawn_finisher(engine, task, label):
        # The edits are done and only the git sequence is missing. Retrying the
        # whole engineer here re-reads the codebase, re-plans, and re-implements
        # work that already exists on disk — at engineer rates — to reach the
        # five mechanical calls it ran out of budget for.
        #
        # The finisher now owns this task: it advances to PR_OPEN on success and
        # performs the retry below itself on failure. Returning False above
        # means none was started and this falls through unchanged.
        return
    else:
        # Either subtasks incomplete or engineer didn't create a PR — retry
        reason = "agent finished with incomplete subtasks" if not subtasks_done else "agent finished without creating a PR"
        await _retry_or_fail(engine, task.id, reason, label)


async def _spawn_finisher(engine: JobEngine, task: Task, label: str) -> bool:
    """Decide whether a finisher can run, and start it in the BACKGROUND.

    Returns True when one was started, meaning the caller must not also retry —
    ownership of the task has passed to the spawned run, which handles both
    outcomes itself.

    Everything awaited here is a cheap database read, deliberately. One of the
    two callers of `_try_complete_task` is the poll loop's orphan recovery,
    which drives job advancement, review checks and deploy monitoring for the
    whole engine; awaiting an LLM agent there would stall all of it for minutes.
    `run_engineer` is spawned for exactly this reason and the finisher gets the
    same treatment.

    Never raises. It sits on the path that decides whether a task advances,
    retries or fails, and an optimisation that throws would take the retry logic
    with it and strand the task in IN_PROGRESS with no owner.
    """
    try:
        existing = await engine.db.get_agents_for_job(task.job_id)
        if any(a.task_id == task.id and a.role == AgentRole.FINISHER for a in existing):
            # One shot per task. A finisher that failed to produce a PR will
            # fail the same way again, and the no-PR condition that got us here
            # is still true afterwards — without this it re-fires forever
            # instead of falling back to a real retry.
            logger.info("%s: task %s already had a finisher — falling back to retry", label, task.id)
            return False

        job = await engine.db.get_job(task.job_id)
        if not job:
            return False
        project, service = engine._resolve_service(task.service)
        if not service:
            logger.warning("%s: no service resolved for task %s — cannot run finisher", label, task.id)
            return False

        agent = Agent(
            job_id=job.id,
            role=AgentRole.FINISHER,
            task_id=task.id,
            model=resolve_model(engine.config, job.difficulty, is_finisher=True),
        )
        agent = await engine.db.create_agent(agent)
        await engine.db.record_event(job.id, "agent_launched", "engine", f"agent={agent.id} role=finisher task={task.id} action=finish")
        await engine._nats_agent_status(job.id, agent.id, str(AgentRole.FINISHER), "launched")
        logger.info("%s: task %s has edits but no PR — launching finisher %s (%s)", label, task.id, agent.id[:8], agent.model)

        engine._spawn(_finish_task(engine, job, task, project, service, agent, label), name=f"finish-{task.id[:8]}")
        return True
    except Exception:
        logger.exception("%s: could not start finisher for task %s — falling back to retry", label, task.id)
        return False


async def _finish_task(engine: JobEngine, job: Job, task: Task, project, service, agent: Agent, label: str) -> None:
    """Run the cheap agent that does only branch/commit/push/create_pr/report_pr.

    The engineer's failure mode is not that it cannot write the code — three
    measured runs wrote the code and then died before git, because the git
    sequence sits at the END of a turn budget the edits have already consumed.
    Retrying the engineer to recover it re-does the expensive half to reach the
    cheap half. This does the cheap half on its own.

    Owns the outcome, because it runs after its caller has returned: on success
    the task advances to PR_OPEN, and on failure it performs the retry that
    `_try_complete_task` skipped. Success is judged by re-reading the task for a
    pr_url rather than by the agent's exit status — an agent can finish cleanly
    having never called report_pr, and that has delivered nothing.
    """
    try:
        # Override the role on a COPY. run_agent resolves both the prompt and
        # the tool set from task.agent_role, and the row in the database must
        # keep saying backend_engineer — that is what the task is, and the retry
        # accounting and service-ownership checks read it.
        finisher_task = task.model_copy(update={"agent_role": AgentRole.FINISHER})

        context = (
            f"The engineer working on this task has stopped. Its changes are in the working tree "
            f"at {service.repo_path}, either uncommitted or committed but unpushed.\n\n"
            f"Task: {task.title}\n{task.description}\n\n"
            f"Branch to use if one is not already checked out: {task.branch_name or f'feat/job-{job.id[:8]}/{task.service}'}\n"
            f"Base branch: {service.default_branch}"
        )

        result_agent = await engine._run_in_process(job, finisher_task, agent, project, service, context)

        refreshed = await engine.db.get_task(task.id)
        if refreshed and refreshed.pr_url:
            try:
                await engine.db.update_task(task.id, status=TaskStatus.PR_OPEN, agent_role="")
                logger.info("%s: finisher opened PR for task %s -> PR_OPEN", label, task.id)
                await _label_minions_mr(engine, refreshed)
                return
            except InvalidTransitionError as e:
                logger.warning("%s: finisher got a PR but transition was rejected for %s: %s", label, task.id, e)
                return

        logger.warning(
            "%s: finisher %s ended %s without reporting a PR for task %s — retrying the engineer",
            label,
            agent.id[:8],
            result_agent.status if result_agent else "unknown",
            task.id,
        )
    except Exception:
        logger.exception("%s: finisher failed for task %s — retrying the engineer", label, task.id)

    await _retry_or_fail(engine, task.id, "finisher could not open a PR", label)


async def _retry_or_fail(engine: JobEngine, task_id: str, reason: str, label: str) -> None:
    """Send a task back for another attempt, or fail it when none remain.

    Shared by `_try_complete_task` and the finisher's fallback so the two cannot
    drift — the finisher is layered in front of this path, never a replacement
    for it, and a task it could not rescue must land exactly where it would have
    landed before the finisher existed.

    Re-reads the task rather than trusting a caller's copy: the finisher path
    holds a reference from before an agent ran, and `attempt` may have moved.
    """
    current = await engine.db.get_task(task_id)
    if not current or current.status != TaskStatus.IN_PROGRESS:
        logger.debug("%s: task %s is no longer in progress — not retrying", label, task_id)
        return

    if current.attempt < current.max_attempts:
        try:
            await engine.db.update_task(task_id, status=TaskStatus.FAILED, agent_role="", error=reason)
            await engine.db.update_task(task_id, status=TaskStatus.PENDING, agent_role="", attempt=current.attempt + 1)
            logger.info("%s: task %s — %s, retrying (attempt %d)", label, task_id, reason, current.attempt + 1)
        except InvalidTransitionError as e:
            logger.warning("%s: could not retry task %s: %s", label, task_id, e)
        return

    try:
        await engine.db.update_task(task_id, status=TaskStatus.FAILED, agent_role="", error=f"{reason}, max attempts reached")
        logger.warning("%s: task %s — %s and no retries left, marking FAILED", label, task_id, reason)
    except InvalidTransitionError as e:
        logger.warning("%s: could not fail task %s: %s", label, task_id, e)


# mergeable_state values that mean GitHub would accept the merge.
# `unstable` = a NON-required check is failing; branch protection permits it, so
# overriding that here would be this gate second-guessing the ruleset.
MERGEABLE_STATES = {"clean", "unstable", "has_hooks"}

# Why each refusal, for a log line that points at the actual problem.
BLOCKING_STATES = {
    "blocked": "required checks or reviews not satisfied",
    "dirty": "merge conflicts with the base branch",
    "behind": "base branch has moved and strict checks are required",
    "draft": "pull request is still a draft",
}


async def _ci_gate_passes(engine: JobEngine, project, provider, mr_id: str, target_branch: str) -> tuple[bool, str]:
    """Whether an agent PR may be merged.

    Two layers, and the second one is not ours:

    1. The repo must HAVE required checks. GitHub does not enforce anything on an
       unprotected branch, so this is the only thing standing between an agent
       and an ungated repo. Fails CLOSED — deliberate pressure on the
       least-verified repos, the inverse of renovate's should_auto_merge where an
       empty ci_status counts as success.

    2. Whether those checks are green is GitHub's call, read via mergeable_state
       and ultimately enforced by branch protection refusing the merge —
       server-side, even for the App that opened the PR.

    Deliberately no longer reads check-runs. That needs a Checks:read grant the
    App does not have, and adding it requires the org to accept the permission.
    It was duplicating a judgement GitHub already makes, and a duplicated
    judgement is one that can drift. mergeable_state arrives with Pull
    requests:read and folds required checks, required reviews and conflicts into
    one value.

    mergeable_state is computed asynchronously, so `unknown` is normal right
    after a push. That is not treated as failure: the merge is attempted and
    branch protection decides. Blocking on `unknown` would strand PRs on a
    timing artefact.
    """
    if not engine.config.require_ci_pass:
        return True, "CI gate disabled (require_ci_pass=false)"

    for method in ("get_required_checks", "get_merge_state"):
        if not hasattr(provider, method):
            return False, f"provider cannot evaluate CI ({method} unavailable) — blocking"

    try:
        required = await provider.get_required_checks(project.project_id, target_branch)
    except Exception as e:
        return False, f"could not read branch rules: {str(e)[:120]}"

    if not required:
        return False, (f"{project.project_id}@{target_branch} has no required status checks — blocking agent merge until the repo is gated")

    try:
        state = await provider.get_merge_state(project.project_id, mr_id)
    except Exception as e:
        return False, f"could not read merge state: {str(e)[:120]}"

    if state in BLOCKING_STATES:
        return False, f"GitHub reports mergeable_state={state} ({BLOCKING_STATES[state]}); required: {required}"

    if state in MERGEABLE_STATES:
        return True, f"GitHub reports mergeable_state={state}; required checks satisfied: {required}"

    # `unknown` and anything GitHub adds later. Let the merge attempt decide
    # rather than inventing a verdict — branch protection is still in force.
    return True, f"mergeable_state={state} (not yet computed) — deferring to branch protection on merge; required: {required}"


async def _retry_or_fail_review(engine: JobEngine, task: Task, reason: str) -> None:
    """Send a task back for another review attempt, or fail it for a human.

    Used when a review did not produce a usable result. Never advances the task
    toward merge — the point is that no one has actually approved this code.
    """
    if task.attempt < task.max_attempts:
        # Bump the counter WITHOUT a status change first. update_task validates
        # transitions and pr_open -> pr_open is illegal, so folding both into one
        # call meant the whole update — including the attempt increment — was
        # rejected and swallowed by the handler below. The retry then looked like
        # it happened and did nothing.
        try:
            await engine.db.update_task(task.id, agent_role="", attempt=task.attempt + 1, error=reason)
        except (InvalidTransitionError, PreconditionError) as e:
            logger.warning("Could not record review retry for task %s: %s", task.id, e)

        if task.status != TaskStatus.PR_OPEN:
            try:
                await engine.db.update_task(task.id, status=TaskStatus.PR_OPEN, agent_role="")
            except (InvalidTransitionError, PreconditionError) as e:
                logger.warning("Could not return task %s to PR_OPEN: %s", task.id, e)

        logger.info("Task %s queued for review attempt %d: %s", task.id, task.attempt + 1, reason)
        return

    try:
        await engine.db.update_task(task.id, status=TaskStatus.FAILED, agent_role="", error=f"{reason} (no review attempts left)")
        logger.error("Task %s FAILED — %s, and no attempts remain", task.id, reason)
    except (InvalidTransitionError, PreconditionError) as e:
        logger.warning("Could not fail task %s: %s", task.id, e)


async def _within_rate_caps(engine: JobEngine, job: Job) -> bool:
    """True if starting this job stays inside the hourly and monthly caps.

    Counts jobs *created* in the window rather than jobs started, so a burst of
    intake cannot slip through by being admitted faster than it is counted.
    """
    from datetime import datetime, timedelta

    windows = (
        ("hour", engine.config.max_jobs_per_hour, timedelta(hours=1)),
        ("month", engine.config.max_jobs_per_month, timedelta(days=30)),
    )

    for label, cap, delta in windows:
        if cap <= 0:
            continue
        since = (datetime.now(UTC) - delta).isoformat()
        count = await engine.db.count_jobs_since(since)
        if count > cap:
            message = f"Job rate cap reached: {count} jobs in the last {label} exceeds the cap of {cap} — deferring job {job.id}"
            logger.warning(message)
            await engine.db.record_event(job.id, "job_rate_cap_deferred", "engine", message)
            return False

    return True


async def launch_spec_analyst(engine: JobEngine, job: Job):
    """Launch the spec analyst agent to refine the raw spec."""
    if await engine._has_running_agent(job.id, AgentRole.SPEC_ANALYST):
        return

    # Admission control. This is the first agent on a job, so gating here gates
    # the whole job — the cost ceilings bound what one job spends, the rate caps
    # bound how many get to spend at all. Over-cap jobs are left at
    # spec_received and start on their own once the window clears.
    if not await _within_rate_caps(engine, job):
        return

    # Classify once, before any expensive agent runs, and reuse the verdict for
    # every agent on this job. A failed classification returns None, which
    # resolve_model treats as "use the default model".
    if job.difficulty is None and engine.config.classifier_enabled:
        difficulty, reason = await classify_difficulty(job.spec, engine.config)
        if difficulty is not None:
            job.difficulty = difficulty
            await engine.db.update_job_difficulty(job.id, difficulty)
            await engine.db.record_event(job.id, "difficulty_classified", "classifier", reason)

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

    agent = Agent(job_id=job.id, role=AgentRole.SPEC_ANALYST, task_id=task.id, model=resolve_model(engine.config, job.difficulty))
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
        # Mark the spec analyst task as done
        try:
            await engine.db.update_task(task.id, status=TaskStatus.DONE, agent_role="")
            logger.info("Spec analyst completed, task %s -> DONE", task.id)
        except InvalidTransitionError as e:
            logger.warning("Rejected task transition for %s: %s", task.id, e)
        # If arbiter not enabled, check if spec analyst advanced the job
        if not engine.config.arbiter_enabled:
            updated_job = await engine.db.get_job(job.id)
            if updated_job and updated_job.status == JobStatus.SPEC_RECEIVED:
                logger.warning("Spec analyst completed but didn't submit refined spec for job %s, advancing with raw spec", job.id)
                await engine.db.update_job_status(job.id, JobStatus.SPEC_READY)
    else:
        await engine.db.update_task(task.id, status=TaskStatus.FAILED, agent_role="", error=f"Spec analyst failed: {result_agent.error or 'unknown'}")
        await engine.db.update_job_status(job.id, JobStatus.FAILED, error=f"Spec analyst failed: {result_agent.error or 'unknown'}")
        await engine._on_job_terminal(job.id)


async def launch_arbiter(engine: JobEngine, job: Job):
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

    agent = Agent(job_id=job.id, role=AgentRole.ARBITER, task_id=task.id, model=resolve_model(engine.config, job.difficulty))
    agent = await engine.db.create_agent(agent)

    logger.info("Launching arbiter for job %s", job.id)
    await engine.db.record_event(job.id, "agent_launched", "engine", f"agent={agent.id} role=arbiter")
    await engine._nats_agent_status(job.id, agent.id, "arbiter", "launched")
    await engine._trello_comment(job, f"Arbiter started (agent={agent.id[:8]})")

    # Build context with available services so the arbiter knows valid service names
    available_services = []
    for project in engine.registry.values():
        if project.services:
            for svc_name, svc in project.services.items():
                lang_str = f" ({svc.language})" if svc.language else ""
                available_services.append(f"- `{svc_name}`{lang_str} — project: {project.name}")
    services_context = None
    if available_services:
        services_context = "## Available Services\n\nWhen creating tasks, use one of these service names:\n" + "\n".join(available_services)

    if engine._k8s_enabled:
        prompt = engine._maybe_dry_run(build_agent_prompt(job, task, None, None, services_context))
        await engine._dispatch_k8s(job, agent, AgentRole.ARBITER, prompt, engine._default_working_dir())
        return

    result_agent = await engine._run_in_process(job, task, agent, None, None, context=services_context)

    if result_agent.status == "done":
        # Mark the arbiter task as done
        try:
            await engine.db.update_task(task.id, status=TaskStatus.DONE, agent_role="")
            logger.info("Arbiter completed, task %s -> DONE", task.id)
        except InvalidTransitionError as e:
            logger.warning("Rejected task transition for %s: %s", task.id, e)
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
        await engine.db.update_task(task.id, status=TaskStatus.FAILED, agent_role="", error=f"Arbiter failed: {result_agent.error or 'unknown'}")
        await engine.db.update_job_status(job.id, JobStatus.FAILED, error=f"Arbiter failed: {result_agent.error or 'unknown'}")
        await engine._on_job_terminal(job.id)


async def launch_engineers(engine: JobEngine, job: Job):
    """Launch engineering agents, sequential per service, parallel across services.

    Tasks targeting the same service run one at a time to avoid rate limits
    and conflicting changes. Tasks for different services run in parallel.
    """
    # Gate: don't launch engineers while the arbiter is still running
    if await engine._has_running_agent(job.id, AgentRole.ARBITER):
        logger.debug("Job %s: arbiter still running, deferring engineer launch", job.id)
        return

    engineer_roles = {AgentRole.BACKEND_ENGINEER, AgentRole.FRONTEND_ENGINEER, AgentRole.DATABASE_ENGINEER}

    # A service is "busy" if any engineer task is actively running through its lifecycle
    # (IN_PROGRESS, PR_OPEN, IN_REVIEW). Tasks in PR_OPEN/IN_REVIEW still own the service
    # slot because they may come back for revisions. PENDING tasks are waiting, not active.
    active_statuses = {TaskStatus.IN_PROGRESS, TaskStatus.PR_OPEN, TaskStatus.IN_REVIEW}
    job_tasks = await engine.db.get_tasks(job.id)
    busy_services = {t.service for t in job_tasks if t.agent_role in engineer_roles and t.status in active_statuses}

    pending_tasks = [t for t in job_tasks if t.agent_role in engineer_roles and t.status == TaskStatus.PENDING]

    fresh_tasks = [t for t in pending_tasks if t.attempt <= 1]
    retry_tasks = [t for t in pending_tasks if t.attempt > 1]

    # Pick up tasks needing revisions (in_progress with changes_requested).
    # Prefix match, not equality: the single-reviewer path writes a bare
    # "changes_requested", but the specialist fan-out writes
    # "changes_requested: <which reviewers objected>". Testing for equality
    # silently dropped every fan-out revision on the floor.
    in_progress_eng = [t for t in job_tasks if t.agent_role in engineer_roles and t.status == TaskStatus.IN_PROGRESS]
    revision_tasks = [t for t in in_progress_eng if (t.review_status or "").startswith("changes_requested")]

    actionable_tasks = fresh_tasks + retry_tasks + revision_tasks
    if not actionable_tasks:
        real_tasks = [t for t in job_tasks if t.service not in ("_spec", "_arbiter")]
        if not real_tasks:
            logger.info("Job %s has no tasks -- arbiter determined no work needed", job.id)
            await engine.db.update_job_status(job.id, JobStatus.NO_WORK_NEEDED)
            await engine._trello_comment(job, "No tasks needed -- arbiter determined no changes required.")
            return
        return

    # Compare-and-swap, not a blind write: this transition is the gate for the
    # launch loop below. If another engine already moved the job out of
    # TASKS_CREATED it has taken ownership of dispatch, and continuing here would
    # start a second set of engineers on the same tasks.
    #
    # ENGINE_ENABLED should already guarantee a single engine per deployment; this
    # is the backstop for the case that guarantee is broken by hand — a scaled-up
    # replica count, or a local `--server` pointed at the same database.
    won = await engine.db.update_job_status(job.id, JobStatus.DEV_IN_PROGRESS, expected_status=JobStatus.TASKS_CREATED)
    if not won:
        logger.info("Job %s already advanced past tasks_created by another engine — skipping launch", job.id)
        return

    # Launch one task per service (first pending wins, rest wait for next poll)
    launched_services = set()
    for t in fresh_tasks:
        if t.service in busy_services or t.service in launched_services:
            continue
        launched_services.add(t.service)
        engine._spawn(run_engineer(engine, job, t, is_revision=False), name=f"eng-{t.id[:8]}")
    for t in retry_tasks:
        if t.service in busy_services or t.service in launched_services:
            continue
        launched_services.add(t.service)
        engine._spawn(run_engineer(engine, job, t, is_retry=True), name=f"eng-retry-{t.id[:8]}")
    for t in revision_tasks:
        if t.service in busy_services or t.service in launched_services:
            continue
        launched_services.add(t.service)
        engine._spawn(run_engineer(engine, job, t, is_revision=True), name=f"eng-rev-{t.id[:8]}")


async def run_engineer(engine: JobEngine, job: Job, task: Task, is_revision: bool = False, is_retry: bool = False):
    """Run a single engineering agent for a task."""
    project, service = engine._resolve_service(task.service)
    if not service:
        logger.error("Unknown service %s for task %s", task.service, task.id)
        await engine.db.update_task(task.id, status=TaskStatus.FAILED, agent_role="", error=f"Unknown service: {task.service}")
        return

    context = None

    if is_retry:
        checkpoint_summary = await build_checkpoint_summary(engine, task.id)
        prior_error = task.error or "Unknown error"
        branch_name = task.branch_name
        if not branch_name:
            short_job_id = job.id[:8]
            slug = task.title.lower().replace(" ", "-")[:30]
            branch_name = f"feat-job-{short_job_id}-{slug}"

        # Only claim the task if something else has not already claimed it.
        #
        # The poll loop sets PENDING -> IN_PROGRESS *before* spawning this
        # coroutine, so on a retry the task is already IN_PROGRESS by the time
        # we get here. Unconditionally writing IN_PROGRESS again is an illegal
        # same-status transition, and the handler below returns — so the agent
        # was never created and the retry did nothing.
        #
        # That made retries inert from the start. Attempt 1 (is_retry=False)
        # ran; every later attempt aborted in milliseconds, which is why job
        # 095146b8 recorded three attempts against a single agent row, and why
        # d1925a3f burned its whole retry budget in 60-second cycles without
        # spending a cent after the first agent died.
        updates = {"branch_name": branch_name}
        current = await engine.db.get_task(task.id)
        if not current or current.status != TaskStatus.IN_PROGRESS:
            updates["status"] = TaskStatus.IN_PROGRESS

        try:
            await engine.db.update_task(task.id, **updates)
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
        # Fresh task — generate branch name per job+service so sequential tasks for the
        # same service share one branch (and one MR).
        short_job_id = job.id[:8]
        branch_name = f"feat-job-{short_job_id}-{task.service}"

        # Inherit PR from a prior completed task on the same service+branch so the
        # engineer pushes to the existing MR instead of creating a new one.
        update_kwargs: dict = {"branch_name": branch_name}
        all_job_tasks = await engine.db.get_tasks(job.id)
        prior = [t for t in all_job_tasks if t.service == task.service and t.id != task.id and t.pr_url and t.branch_name == branch_name]
        if prior:
            update_kwargs["pr_url"] = prior[-1].pr_url
            update_kwargs["pr_number"] = prior[-1].pr_number
            update_kwargs["mr_id"] = prior[-1].mr_id

        current_task = await engine.db.get_task(task.id)
        if current_task and current_task.status != TaskStatus.IN_PROGRESS:
            try:
                await engine.db.update_task(task.id, status=TaskStatus.IN_PROGRESS, **update_kwargs)
            except InvalidTransitionError as e:
                logger.warning("Rejected task transition for %s: %s", task.id, e)
                return
        else:
            await engine.db.update_task(task.id, **update_kwargs)

    agent = Agent(job_id=job.id, role=task.agent_role, task_id=task.id, model=resolve_model(engine.config, job.difficulty, is_engineer=True))
    agent = await engine.db.create_agent(agent)

    action = "retry" if is_retry else ("revision" if is_revision else "development")
    event_detail = f"agent={agent.id} role={task.agent_role} task={task.id} action={action}"
    await engine.db.record_event(job.id, "agent_launched", "engine", event_detail)
    await engine._nats_agent_status(job.id, agent.id, str(task.agent_role), "launched")
    await engine._trello_comment(job, f"{task.agent_role} started {action} on {task.service} (agent={agent.id[:8]})")

    # Build knowledge context from memory system (when enabled)
    knowledge_ctx = None
    if engine.memory_store and engine.config.memory_enabled:
        try:
            from agent_memory.context import build_knowledge_context

            knowledge_ctx = await build_knowledge_context(
                engine.memory_store, task.service, task.description, max_tokens=engine.config.memory_l3_token_budget
            )
        except Exception as e:
            logger.warning("Failed to build knowledge context: %s", e)

    if engine._k8s_enabled:
        prompt = engine._maybe_dry_run(build_agent_prompt(job, task, project, service, context, knowledge_context=knowledge_ctx))
        await engine._dispatch_k8s(job, agent, str(task.agent_role), prompt, service.repo_path, service=service)
        return

    result_agent = await engine._run_in_process(job, task, agent, project, service, context, knowledge_context=knowledge_ctx)

    if result_agent.status == "done":
        if is_revision:
            # After a successful revision, move task back to PR_OPEN for re-review
            current_task = await engine.db.get_task(task.id)
            if current_task and current_task.status not in (TaskStatus.PR_OPEN, TaskStatus.IN_REVIEW, TaskStatus.MERGED, TaskStatus.DONE):
                try:
                    await engine.db.update_task(task.id, status=TaskStatus.PR_OPEN, agent_role="", review_status="revision_complete")
                    logger.info("Revision agent completed, task %s -> PR_OPEN for re-review", task.id)
                except InvalidTransitionError as e:
                    logger.warning("Rejected task transition for %s: %s", task.id, e)
        else:
            # Fresh/retry task — mark done only if all subtasks are complete
            current_task = await engine.db.get_task(task.id)
            if current_task and current_task.status == TaskStatus.IN_PROGRESS:
                await _try_complete_task(engine, current_task, "run_engineer")
    else:
        try:
            await engine.db.update_task(task.id, status=TaskStatus.FAILED, agent_role="", error=(result_agent.error or "unknown")[:200])
        except InvalidTransitionError:
            logger.warning("Could not mark task %s as failed", task.id)


async def get_review_feedback(engine: JobEngine, job_id: str, task: Task) -> str:
    """Fetch review feedback messages for a task from the code_reviewer."""
    messages = await engine.db.get_messages(job_id)
    feedback_parts = []
    for msg in messages:
        if msg.from_role == AgentRole.CODE_REVIEWER and msg.to_role == task.agent_role:
            feedback_parts.append(msg.content)
    if feedback_parts:
        return "\n\n---\n\n".join(feedback_parts)

    # Nothing in the messages table — which is the NORMAL case, not an edge one.
    #
    # The specialist fan-out posts its findings as GitHub PR reviews and writes
    # nothing here, so this lookup has always come up empty and every revision
    # agent received the fallback string below instead of the review. It was
    # then told to revise without being told what was wrong: on job 263b8b3e
    # three reviewers unanimously blocked PR #81 with file-and-line-cited
    # findings, the revision engineer ran to completion (627k tokens, status
    # done) and committed nothing, and the dedup guard correctly refused to
    # re-review an unchanged PR. The loop looked healthy at every layer while
    # accomplishing nothing.
    #
    # So go and read the reviews from where they actually live.
    pr_reviews = await _fetch_pr_review_bodies(engine, task)
    if pr_reviews:
        return pr_reviews

    # Genuinely nothing to act on. Say so plainly rather than implying the
    # reviewer simply had no comments — a revision agent with no feedback
    # cannot succeed, and should not burn a full budget discovering that.
    return (
        "FEEDBACK LOOKUP FAILED: the reviewers requested changes but their comments could not be "
        "retrieved from either the message log or the pull request. Do not guess at what to change. "
        "Report this failure instead of making speculative edits."
    )


async def _fetch_pr_review_bodies(engine: JobEngine, task: Task) -> str:
    """Review bodies for `task`'s PR, straight from the git provider.

    The reviewers' actual output lives on the PR, not in the messages table.
    Returns "" on any failure — a provider hiccup must not be mistaken for
    "the reviewer had nothing to say", which is why the caller distinguishes
    empty-from-here from feedback-found.
    """
    import json as _json
    import re as _re
    import subprocess

    url = task.pr_url or task.mr_url or ""
    match = _re.search(r"github\.com/([^/\s]+/[^/\s]+)/pull/(\d+)", url)
    if not match:
        return ""
    repo, number = match.group(1), match.group(2)

    try:
        result = subprocess.run(
            ["gh", "api", f"repos/{repo}/pulls/{number}/reviews", "--jq", '[.[] | select(.body != "") | {state, body}]'],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            logger.warning("Could not fetch PR reviews for %s#%s: %s", repo, number, (result.stderr or "")[:160])
            return ""
        reviews = _json.loads(result.stdout or "[]")
    except Exception as e:
        logger.warning("Could not fetch PR reviews for %s#%s: %s", repo, number, e)
        return ""

    blocking = [r for r in reviews if r.get("state") == "CHANGES_REQUESTED"]
    chosen = blocking or reviews
    if not chosen:
        return ""

    logger.info("Loaded %d PR review(s) as revision feedback for task %s", len(chosen), task.id)
    return "\n\n---\n\n".join(r.get("body", "") for r in chosen)


async def _run_one_specialist(
    engine: JobEngine,
    job: Job,
    task: Task,
    specialty: str,
    project,
    service,
    mr_id: str,
    mr_info: dict,
    provider,
    review_context: str,
) -> tuple[str, str | None]:
    """Run a single expert reviewer. Returns (specialty, verdict-or-None).

    Never raises: one specialist blowing up must not take the others with it.
    A None verdict is a real signal — aggregate_verdicts fails closed on it.
    """
    from ..reviewers import load_persona

    reviewer_task = await engine.db.create_task(
        Task(
            job_id=job.id,
            title=f"[{specialty}] Review PR for {task.title}",
            description=f"Review PR {task.pr_url or 'pending'}",
            service=task.service,
            agent_role=AgentRole.CODE_REVIEWER,
            status=TaskStatus.IN_PROGRESS,
            specialty=specialty,
            # Lets the fan-out guard tell "already reviewed THIS revision"
            # from "already reviewed an older one". Without it a revision can
            # never be re-reviewed and the PR stays blocked forever.
            revision_count=task.revision_count,
            mr_url=task.pr_url or "",
            mr_id=mr_id,
            pr_url=task.pr_url or "",
            pr_number=task.pr_number,
        )
    )

    # Reviewers fan out, so they get their own model tier — see resolve_model.
    model = resolve_model(engine.config, job.difficulty, project.model if project else "", is_reviewer=True)
    agent = await engine.db.create_agent(Agent(job_id=job.id, role=AgentRole.CODE_REVIEWER, task_id=reviewer_task.id, model=model))

    await engine.db.record_event(job.id, "agent_launched", "engine", f"agent={agent.id} role=code_reviewer specialty={specialty}")
    await engine._nats_agent_status(job.id, agent.id, "code_reviewer", "launched")

    persona = load_persona(specialty)
    context = f"## Review Target\n\n{review_context}"
    if persona:
        context += f"\n\n## Your Review Lens\n\n{persona}"

    if engine._k8s_enabled:
        prompt = engine._maybe_dry_run(build_agent_prompt(job, reviewer_task, project, service, context))
        working_dir = service.repo_path if service else "."
        await engine._dispatch_k8s(job, agent, AgentRole.CODE_REVIEWER, prompt, working_dir, service=service)
        return specialty, None

    try:
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
            agent=agent,
        )
    except Exception as e:
        logger.error("Reviewer %s failed for task %s: %s", specialty, task.id, e, exc_info=True)
        try:
            await engine.db.update_task(reviewer_task.id, status=TaskStatus.FAILED, agent_role="", error=str(e)[:200])
        except InvalidTransitionError, PreconditionError:
            pass
        return specialty, None

    verdict = getattr(result_agent, "_review_verdict", None)
    terminal = TaskStatus.DONE if result_agent.status == "done" else TaskStatus.FAILED
    try:
        await engine.db.update_task(reviewer_task.id, status=terminal, agent_role="", verdict=verdict or "")
    except (InvalidTransitionError, PreconditionError) as e:
        logger.warning("Could not mark reviewer task %s as %s: %s", reviewer_task.id, terminal, e)

    if result_agent.status != "done":
        return specialty, None

    logger.info("Reviewer %s verdict for task %s: %s", specialty, task.id, verdict)
    return specialty, verdict


async def run_task_review(engine: JobEngine, job: Job, task: Task):
    """Fan out expert reviewers across a task's PR, then act on their verdict.

    Two always run (api, backend-architecture); the rest fire on signals in the
    diff, so a Python-only PR wakes three specialists rather than five. That
    conditionality is the cost control — each is a full agent run.
    """
    import asyncio

    from ..reviewers import aggregate_verdicts, infer_specialists, skipped_specialists
    from .review import create_engineer_provider, create_reviewer_provider

    # A fan-out either happened for this PR or it did not. Checking per-specialty
    # would let a re-entry add stragglers to a review that already concluded.
    #
    # Reached from the arbiter's `advance_job` remediation, which re-fires every
    # monitor pass while a job looks stuck — that spawned duplicate reviewers
    # before this guard existed ($4.87 for a review needed once).
    existing = await engine.db.get_tasks(job.id)
    # Scoped to the CURRENT revision, not just the PR.
    #
    # Keyed on pr_url alone this blocked re-review forever: once reviewer
    # tasks existed for a PR, no amount of new commits produced a fresh
    # review. Job 7d835e9e hit exactly that - reviewers requested changes,
    # the revision agent received their feedback and pushed a real fix
    # (commit 20342315), and this guard then refused to look at it. The PR
    # sat CHANGES_REQUESTED against a diff that had already been corrected,
    # so it could never become mergeable and the job could never finish.
    #
    # revision_count is stamped onto each reviewer task at creation, so a
    # revision bumping it makes the previous round stop matching and a new
    # fan-out is allowed - while a re-entry within the SAME revision still
    # matches and is still blocked, which is what this guard was built for.
    already = [
        t
        for t in existing
        if t.agent_role == AgentRole.CODE_REVIEWER
        and t.status != TaskStatus.FAILED
        and (t.pr_url or "") == (task.pr_url or "")
        and (t.revision_count or 0) == (task.revision_count or 0)
    ]
    if already:
        logger.info(
            "Review already ran for task %s (PR %s): %d reviewer task(s) — not fanning out again",
            task.id,
            task.pr_url or "pending",
            len(already),
        )
        return

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

    mr_id = task.mr_id or str(task.pr_number or "")
    if not mr_id and task.pr_url:
        import re

        match = re.search(r"/merge_requests/(\d+)", task.pr_url) or re.search(r"/pull/(\d+)", task.pr_url)
        if match:
            mr_id = match.group(1)

    project, service = engine._resolve_service(task.service)

    # Fetch the diff as well as the file list: the DBA trigger keys on SQL and
    # ORM tokens in the diff, which no path pattern reveals.
    provider = None
    mr_info: dict = {}
    changed_files: list[str] = []
    diff = ""
    if project and mr_id:
        try:
            provider = await create_reviewer_provider(project, engine.config)
            changed_files = await provider.get_changed_files(project.project_id, mr_id)
            mr_info = {"project_id": project.project_id, "changed_files": changed_files}
            try:
                diff = await provider.get_diff(project.project_id, mr_id)
            except Exception as e:
                # Degrades the DBA trigger to path-only — a weaker but never-wrong
                # subset. Not worth failing the review over.
                logger.warning("Could not fetch diff for %s#%s: %s", project.project_id, mr_id, str(e)[:120])
        except Exception as e:
            logger.warning("Failed to create provider/fetch MR info for task review %s: %s", task.id, e)
            mr_info = {"project_id": project.project_id if project else "", "changed_files": []}

    specialists = infer_specialists(changed_files, diff)
    skipped = skipped_specialists(specialists)
    logger.info(
        "Review fan-out for task %s: %s%s",
        task.id,
        ", ".join(specialists),
        f" (skipped: {', '.join(skipped)})" if skipped else "",
    )
    await engine.db.record_event(job.id, "review_fanout", "engine", f"task={task.id} ran={','.join(specialists)} skipped={','.join(skipped)}")

    # Per-job spend ceiling. Reviewers call run_agent directly rather than going
    # through _run_in_process, so the job ceiling never applied to them. With one
    # reviewer that was survivable; fanning out four makes it the dominant cost.
    if engine.config.job_cost_limit_usd > 0:
        usage = await engine.db.get_job_usage(job.id)
        spent = float(usage.get("total_cost_usd") or 0.0)
        if spent >= engine.config.job_cost_limit_usd:
            message = (
                f"Job {job.id} has spent ${spent:.2f}, at or over its ${engine.config.job_cost_limit_usd:.2f} limit — refusing to fan out reviewers"
            )
            logger.error(message)
            await engine.db.record_event(job.id, "job_cost_limit_exceeded", "engine", message)
            await _retry_or_fail_review(engine, task, message)
            return

    results = await asyncio.gather(
        *[_run_one_specialist(engine, job, task, specialty, project, service, mr_id, mr_info, provider, review_context) for specialty in specialists],
        return_exceptions=True,
    )

    verdicts: dict[str, str | None] = {}
    for specialty, outcome in zip(specialists, results, strict=False):
        if isinstance(outcome, BaseException):
            logger.error("Reviewer %s raised for task %s: %s", specialty, task.id, outcome)
            verdicts[specialty] = None
        else:
            verdicts[specialty] = outcome[1]

    if engine._k8s_enabled:
        # Dispatched to the cluster; verdicts arrive asynchronously.
        return

    # Re-fetch: the task may have moved on while the fan-out ran.
    current_task = await engine.db.get_task(task.id)
    if not current_task or current_task.status != TaskStatus.IN_REVIEW:
        logger.info("Fan-out finished but task %s is now %s — skipping verdict", task.id, current_task.status if current_task else "gone")
        return

    verdict, reason = aggregate_verdicts(verdicts)
    logger.info("Aggregated review verdict for task %s: %s (%s)", task.id, verdict, reason)
    await engine.db.record_event(job.id, "review_aggregated", "engine", f"task={task.id} verdict={verdict} {reason}")

    if verdict == "request_changes":
        # Covers a genuine objection AND a missing verdict — aggregate_verdicts
        # fails closed, so a crashed specialist lands here rather than approving.
        try:
            await engine.db.update_task(task.id, review_status=f"changes_requested: {reason[:150]}", agent_role="")
            await engine.db.update_task(task.id, status=TaskStatus.IN_PROGRESS, agent_role="")
            logger.info("Review requested changes, task %s -> IN_PROGRESS for revision", task.id)
        except (InvalidTransitionError, PreconditionError) as e:
            logger.warning("Could not transition task %s for revision: %s", task.id, e)
        return

    if verdict == "discuss":
        # No human is watching an autonomous run, so "needs discussion" cannot
        # mean "wait indefinitely". Treat it as blocking and leave it for a human.
        message = f"Reviewers want discussion, not approval: {reason}"
        logger.warning("%s (task %s)", message, task.id)
        await _retry_or_fail_review(engine, task, message)
        return

    # Approved.
    if project and project.auto_merge and mr_id:
        try:
            # Deliberately NOT the reviewer provider — that identity has read-only
            # Contents. Merging writes to the base branch and --delete-branch
            # removes a ref. The engineer App already has write, and GitHub only
            # forbids an identity *approving* its own PR, never merging one.
            merge_provider = await create_engineer_provider(project, engine.config)

            target_branch = (service.default_branch if service else "") or "main"
            ci_ok, ci_reason = await _ci_gate_passes(engine, project, merge_provider, mr_id, target_branch)

            if not ci_ok:
                logger.warning("Auto-merge BLOCKED for task %s: %s", task.id, ci_reason)
                await engine.db.record_event(job.id, "auto_merge_blocked_ci", "engine", f"task={task.id} {ci_reason}")
                merge_result = {"merged": False, "error": f"CI gate: {ci_reason}"}
            else:
                logger.info("CI gate passed for task %s: %s", task.id, ci_reason)
                merge_result = await merge_provider.merge_mr(project.project_id, mr_id)

                # Branch protection refusing the merge IS the gate — the
                # pre-check is only a hint, and mergeable_state can be `unknown`
                # when it runs. Record the refusal distinctly so it does not read
                # as a transient API failure.
                if not merge_result.get("merged"):
                    detail = str(merge_result.get("error", "unknown"))
                    await engine.db.record_event(job.id, "auto_merge_refused", "engine", f"task={task.id} {detail[:200]}")
                    logger.warning("Merge refused for task %s (branch protection or conflict): %s", task.id, detail[:200])

            if merge_result.get("merged"):
                logger.info("Auto-merged MR %s for task %s", mr_id, task.id)
        except Exception as e:
            logger.warning("Auto-merge error for task %s: %s", task.id, e)

    try:
        await engine.db.update_task(task.id, status=TaskStatus.MERGED, agent_role="")
        logger.info("Review approved (%s), task %s -> MERGED", reason, task.id)
    except (InvalidTransitionError, PreconditionError) as e:
        logger.warning("Could not transition task %s to MERGED after approval: %s", task.id, e)


async def manage_dev_tasks(engine: JobEngine, job: Job):
    """Per-task lifecycle manager: review launches, revision cycles, job advancement.

    Enforces sequential execution per service — only one engineer runs per
    service at a time. Different services can run in parallel.
    """
    engineer_roles = {AgentRole.BACKEND_ENGINEER, AgentRole.FRONTEND_ENGINEER, AgentRole.DATABASE_ENGINEER}
    tasks = await engine.db.get_tasks(job.id)
    dev_tasks = [t for t in tasks if t.agent_role in engineer_roles]

    if not dev_tasks:
        return

    terminal_statuses = {TaskStatus.MERGED, TaskStatus.DONE, TaskStatus.FAILED}

    # A service is "busy" if any engineer task is actively running through its lifecycle
    # (IN_PROGRESS, PR_OPEN, IN_REVIEW). Tasks in PR_OPEN/IN_REVIEW still own the service
    # slot because they may come back for revisions. PENDING tasks are waiting, not active.
    active_statuses = {TaskStatus.IN_PROGRESS, TaskStatus.PR_OPEN, TaskStatus.IN_REVIEW}
    busy_services = {t.service for t in dev_tasks if t.status in active_statuses}
    launched_services = set()

    for task in dev_tasks:
        if task.status == TaskStatus.PR_OPEN:
            # PR just opened — transition to in_review and spawn reviewer
            try:
                await engine.db.update_task(task.id, status=TaskStatus.IN_REVIEW, agent_role="")
            except InvalidTransitionError as e:
                logger.warning("Could not transition task %s to in_review: %s", task.id, e)
                continue
            engine._spawn(run_task_review(engine, job, task), name=f"review-{task.id[:8]}")

        elif task.status == TaskStatus.PENDING:
            # Only launch if no other task for this service is running
            if task.service in busy_services or task.service in launched_services:
                continue
            launched_services.add(task.service)
            # Claim the task immediately so the next poll doesn't spawn a duplicate
            try:
                await engine.db.update_task(task.id, status=TaskStatus.IN_PROGRESS, agent_role="")
            except InvalidTransitionError as e:
                logger.warning("Could not claim task %s: %s", task.id, e)
                continue
            is_retry = task.attempt > 1
            engine._spawn(
                run_engineer(engine, job, task, is_retry=is_retry),
                name=f"eng-{'retry' if is_retry else 'recover'}-{task.id[:8]}",
            )

        elif task.status == TaskStatus.IN_PROGRESS and (task.review_status or "").startswith("changes_requested"):
            # Reviewer requested changes — launch revision engineer.
            #
            # Prefix match, not equality. run_task_review writes
            # "changes_requested: <reason>" after aggregating the specialist
            # fan-out; only the older single-reviewer path writes the bare
            # string. Under equality this branch never fired for a fan-out
            # verdict, so control fell through to the IN_PROGRESS orphan
            # recovery below -- which sees the *previous* engineer agent still
            # marked done, concludes "agent finished but the task was never
            # updated", and promotes the task to PR_OPEN. The revision intent
            # was erased ~3s after it was recorded, review relaunched, the
            # reviewer-dedup guard correctly refused a second fan-out, and the
            # job wedged at dev_in_progress forever (job 0f90844d).
            #
            # Skip if there's already a running agent for this task
            latest_agent = await engine.db.get_agent_for_task(task.id)
            latest_agent = await engine.db.get_agent_for_task(task.id)
            if latest_agent and latest_agent.status in ("starting", "running"):
                continue
            if task.revision_count >= engine.config.max_revisions:
                logger.warning("Task %s hit max revisions (%d), failing", task.id, engine.config.max_revisions)
                try:
                    await engine.db.update_task(
                        task.id, status=TaskStatus.FAILED, agent_role="", error=f"Max revisions ({engine.config.max_revisions}) exceeded"
                    )
                except InvalidTransitionError:
                    pass
                await engine.db.record_event(job.id, "task_max_revisions", "engine", f"task={task.id} revision_count={task.revision_count}")
                continue
            await engine.db.update_task(task.id, revision_count=task.revision_count + 1, review_status="revision_in_progress")
            await engine.db.record_event(
                job.id,
                "task_revision_requested",
                "engine",
                f"task={task.id} revision={task.revision_count + 1}/{engine.config.max_revisions}",
            )
            engine._spawn(run_engineer(engine, job, task, is_revision=True), name=f"eng-rev-{task.id[:8]}")

        elif task.status == TaskStatus.IN_REVIEW:
            # IN_REVIEW tasks are handled by a spawned reviewer coroutine.
            # But detect stuck reviewers — if the latest agent is starting/failed, recover.
            latest_agent = await engine.db.get_agent_for_task(task.id)
            if latest_agent and latest_agent.status == "starting":
                from datetime import datetime

                started = latest_agent.started_at
                if isinstance(started, str):
                    started = datetime.fromisoformat(started)
                if started and started.tzinfo is None:
                    started = started.replace(tzinfo=UTC)
                age = (datetime.now(UTC) - started).total_seconds() if started else 0
                if age > 120:
                    logger.warning("Reviewer agent %s stuck in 'starting' for %ds — marking failed", latest_agent.id, int(age))
                    await engine.db.update_agent(
                        latest_agent.id, status="failed", finished_at=datetime.now(UTC).isoformat(), error="stuck in starting state"
                    )
                    latest_agent = await engine.db.get_agent_for_task(task.id)

            if latest_agent and latest_agent.status == "failed":
                # Reviewer died — bounce task back to PR_OPEN so it gets re-reviewed
                try:
                    await engine.db.update_task(task.id, status=TaskStatus.PR_OPEN, agent_role="")
                    logger.info("Recovered stuck IN_REVIEW task %s back to PR_OPEN", task.id)
                except InvalidTransitionError as e:
                    logger.warning("Could not recover IN_REVIEW task %s: %s", task.id, e)

        elif task.status == TaskStatus.IN_PROGRESS:
            # Check if the agent is actually dead (orphaned task)
            latest_agent = await engine.db.get_agent_for_task(task.id)

            # Detect stuck 'starting' agents — if started > 2 min ago, it's orphaned
            if latest_agent and latest_agent.status == "starting":
                from datetime import datetime

                started = latest_agent.started_at
                if isinstance(started, str):
                    started = datetime.fromisoformat(started)
                if started and started.tzinfo is None:
                    started = started.replace(tzinfo=UTC)
                age = (datetime.now(UTC) - started).total_seconds() if started else 0
                if age > 120:
                    logger.warning("Agent %s stuck in 'starting' for %ds — marking failed", latest_agent.id, int(age))
                    await engine.db.update_agent(
                        latest_agent.id, status="failed", finished_at=datetime.now(UTC).isoformat(), error="stuck in starting state"
                    )
                    await engine.db.record_event(
                        job.id, "agent_orphaned", "engine", f"agent={latest_agent.id} role={latest_agent.role} reason=stuck_starting"
                    )
                    # Let the next block handle task recovery
                    latest_agent = await engine.db.get_agent_for_task(task.id)

            if latest_agent and latest_agent.status in ("failed", "done"):
                # The agent may be a leftover from the PREVIOUS attempt: a task
                # is claimed (IN_PROGRESS) before run_engineer creates the new
                # agent row, so briefly the newest agent on record is the old
                # finished one. Recovering against it consumes an attempt for
                # work that is already being redone — on job 095146b8 that ate
                # attempt 3 six seconds after attempt 2, and the task failed
                # with max_attempts exhausted having only ever run one agent.
                if _agent_predates_current_attempt(latest_agent, task):
                    logger.debug("orphan recovery: agent %s predates task %s's current attempt — deferring", latest_agent.id, task.id)
                    continue

                if latest_agent.status == "done":
                    # Agent succeeded but task wasn't updated — check subtasks first
                    await _try_complete_task(engine, task, "orphan recovery")
                else:
                    if task.attempt < task.max_attempts:
                        try:
                            await engine.db.update_task(task.id, status=TaskStatus.FAILED, agent_role="", error="agent died without completing")
                            await engine.db.update_task(task.id, status=TaskStatus.PENDING, agent_role="", attempt=task.attempt + 1)
                            logger.info("Orphan recovery: task %s reset to pending (attempt %d)", task.id, task.attempt + 1)
                        except InvalidTransitionError as e:
                            logger.warning("Orphan recovery: could not reset task %s: %s", task.id, e)
                    else:
                        try:
                            await engine.db.update_task(
                                task.id, status=TaskStatus.FAILED, agent_role="", error="max attempts reached after agent death"
                            )
                        except InvalidTransitionError:
                            pass

        elif task.status == TaskStatus.FAILED and task.attempt < task.max_attempts:
            # Failed task with retries remaining — auto-retry
            try:
                await engine.db.update_task(task.id, status=TaskStatus.PENDING, agent_role="", attempt=task.attempt + 1, error=None)
                logger.info("Auto-retry: task %s reset to pending (attempt %d/%d)", task.id, task.attempt + 1, task.max_attempts)
                await engine.db.record_event(job.id, "task_auto_retry", "engine", f"task={task.id} attempt={task.attempt + 1}/{task.max_attempts}")
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


async def build_checkpoint_summary(engine: JobEngine, task_id: str) -> str:
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
