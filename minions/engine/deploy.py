"""Deploy job handlers -- standalone functions receiving the engine instance.

DECISION (2026-08-23): this pipeline does not execute or monitor deployments.
Merge is its terminal responsibility; deployment belongs to each repo's own CD
(ArgoCD and friends), which is where it already lived for every project --
each one runs deploy_target: "none", and the herder on job 7e8b4769 said the
quiet part out loud: "#86 deploys via management-dashboard's own CD".

The machinery this replaces could not have worked even if asked to: the
monitor's report_deploy_status had the monitor's OWN task id injected, so the
engineer tasks parked at DEPLOYING were unmovable by construction, and
check_deployed excluded the one task the monitor could update -- a job with a
real deploy_target would have parked at DEPLOYING forever. Its "deploy
monitoring" was `gh pr checks` against an already-merged PR, GitHub-only,
with a prompt asking for 30-second polling from an agent with no way to wait.
Nothing was lost by removing it, because nothing it promised ever ran.

What remains: MERGED passes straight through to DEPLOYED (the states stay,
for history and for the transition-invariant tests), a deploy_delegated event
records any configured target so the delegation is visible rather than
silent, and check_deployed heals any job a previous release left parked at
DEPLOYING. The engine's DEPLOYED branch owns DONE and _on_job_terminal --
calling it here too was a duplicate S3 upload and archive per job.
"""

import logging
from typing import TYPE_CHECKING

from ..core.models import AgentRole, Job, JobStatus, TaskStatus

if TYPE_CHECKING:
    from .job_engine import JobEngine

logger = logging.getLogger(__name__)

ENGINEER_ROLES = {AgentRole.BACKEND_ENGINEER, AgentRole.FRONTEND_ENGINEER, AgentRole.DATABASE_ENGINEER}

# An engineer task that will never deploy, for any reason. NO_WORK_NEEDED belongs
# here for the same reason DONE does: it is terminal and it is NOT a failure (see
# TaskStatus.NO_WORK_NEEDED in core/models.py) -- the engineer read the code and
# found the change already present, so there is nothing to ship and nothing to wait for.
#
# Omitting it wedged job c2b97f39 for two days. Its engineer tasks were
# {done, no_work_needed}, so `all_done` was False and the job parked in MERGED
# forever -- and get_active_jobs() counts that as active, so with
# max_concurrent_jobs=1 the wedged job held the only slot and intake stopped
# entirely, with no new log line anywhere. Do not narrow this set without
# re-reading that chain.
TERMINAL_DEV_TASK_STATUSES = (TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.NO_WORK_NEEDED)


async def advance_merged_job(engine: JobEngine, job: Job):
    """Complete a MERGED job's tasks and hand deployment to the repo's own CD."""
    tasks = await engine.db.get_tasks(job.id)

    for t in [t for t in tasks if t.status == TaskStatus.MERGED]:
        _, service = engine._resolve_service(t.service)
        target = getattr(service, "deploy_target", None) if service else None
        if isinstance(target, str) and target and target != "none":
            # The target is configuration someone wrote down expecting an
            # actor. The actor is the repo's CD; say so in the record rather
            # than skipping silently.
            await engine.db.record_event(
                job.id, "deploy_delegated", "engine", f"task={t.id} service={t.service} target={target} -- deployment is owned by the repo's own CD"
            )
        await engine.db.update_task(t.id, status=TaskStatus.DONE, agent_role="")

    tasks = await engine.db.get_tasks(job.id)
    dev_tasks = [t for t in tasks if t.agent_role in ENGINEER_ROLES]
    all_done = dev_tasks and all(t.status in TERMINAL_DEV_TASK_STATUSES for t in dev_tasks)
    if all_done:
        logger.info("Job %s: merge complete, deployment delegated to the repos -- advancing to DEPLOYED", job.id)
        await engine.db.update_job_status(job.id, JobStatus.DEPLOYED)


async def check_deployed(engine: JobEngine, job: Job):
    """Heal a job parked at DEPLOYING -- a state nothing produces any more.

    Before the decision above, a job whose service had a real deploy_target
    moved its tasks to DEPLOYING and waited on a monitor that could never
    conclude. Any such job still in the database advances here: DEPLOYING
    tasks complete, a leftover _deploy monitor task is closed, and the job
    moves to DEPLOYED for the engine's DEPLOYED branch to finish.
    """
    tasks = await engine.db.get_tasks(job.id)

    for t in tasks:
        if t.status == TaskStatus.DEPLOYING or (t.service == "_deploy" and t.status in (TaskStatus.PENDING, TaskStatus.IN_PROGRESS)):
            await engine.db.update_task(t.id, status=TaskStatus.DONE, agent_role="")

    await engine.db.record_event(job.id, "deploy_healed", "engine", "job left DEPLOYING by a pre-0.8.53 monitor -- advanced")
    await engine.db.update_job_status(job.id, JobStatus.DEPLOYED)
