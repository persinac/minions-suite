"""A retry must ask the remote about pushed work before inventing a branch.

report_pr verifies before recording, so an attempt that pushed and THEN died
(label failure, refused report) leaves its branch on origin and branch_name
empty in the DB. The old retry generated a fresh name from the task title at
that point, orphaning the pushed work: job 3b8b8ba9's attempt 3 rewrote four
completed subtasks from scratch on a new branch while attempt 1's finished
branch sat on the remote.

These run the retry path under external dispatch — it does all the branch
bookkeeping and then publishes, which is exactly the seam where the herder
inherits branch_name.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from minions.core.models import AgentRole, JobStatus, Task, TaskStatus
from minions.engine.dev import run_engineer
from tests.engine.test_dev import _mock_engine

CLONE_URL = "https://github.com/flippin-balls/wallet-api.git"


async def _retry_task(db, branch_name=None):
    job = await db.create_job("spec")
    for s in (JobStatus.SPEC_READY, JobStatus.TASKS_CREATED, JobStatus.DEV_IN_PROGRESS):
        await db.update_job_status(job.id, s)
    task = await db.create_task(
        Task(job_id=job.id, title="Add retry logic", description="d", service="wallet-api", agent_role=AgentRole.BACKEND_ENGINEER)
    )
    await db.update_task(task.id, status=TaskStatus.IN_PROGRESS, attempt=2)
    if branch_name:
        await db.update_task(task.id, branch_name=branch_name)
    return await db.get_job(job.id), await db.get_task(task.id)


def _external_engine(db):
    engine = _mock_engine(db)
    engine.config.engineer_dispatch = "external"
    project = MagicMock()
    service = MagicMock(clone_url=CLONE_URL, repo_path="/repos/wallet-api", default_branch="main")
    engine._resolve_service = MagicMock(return_value=(project, service))
    return engine


async def _run_retry(db, job, task, remote_branches):
    engine = _external_engine(db)
    finder = AsyncMock(return_value=remote_branches)
    with patch("minions.repos.find_job_branches", new=finder):
        await run_engineer(engine, job, task, is_retry=True)
    return finder


async def _events(db, job_id, event_type):
    return [e for e in await db.get_events(job_id) if e.get("event_type") == event_type]


class TestRetryAsksTheRemote:
    async def test_a_pushed_branch_is_adopted_when_the_db_has_none(self, db):
        job, task = await _retry_task(db)
        pushed = f"feat/job-{job.id[:8]}/wallet-retry-logic"

        finder = await _run_retry(db, job, task, [pushed])

        finder.assert_awaited_once_with(CLONE_URL, job.id[:8])
        assert (await db.get_task(task.id)).branch_name == pushed
        assert await _events(db, job.id, "retry_branch_detected")

    async def test_a_recorded_branch_name_is_not_overridden(self, db):
        """report_pr's verified record outranks a remote guess — but the
        detection is still surfaced for the audit trail."""
        job, task = await _retry_task(db, branch_name="feat-recorded")

        await _run_retry(db, job, task, ["feat-something-else"])

        assert (await db.get_task(task.id)).branch_name == "feat-recorded"
        assert await _events(db, job.id, "retry_branch_detected")

    async def test_no_remote_branches_falls_back_to_the_generated_name(self, db):
        job, task = await _retry_task(db)

        await _run_retry(db, job, task, [])

        refreshed = await db.get_task(task.id)
        assert refreshed.branch_name.startswith(f"feat-job-{job.id[:8]}-")
        assert not await _events(db, job.id, "retry_branch_detected")
