"""A second advance must not turn a merged job into a failed one.

`manage_dev_tasks` ends by writing a job status, and every status it writes is
legal only from DEV_IN_PROGRESS. Nothing serialised the callers, so two advances
could race: both read the job as dev_in_progress, the first wrote MERGED, and
the second attempted `merged -> merged`. That is not an edge in JOB_TRANSITIONS,
so it raised; `manage_dev_node` caught the exception and recorded the job FAILED.

Job 1e7a3d17 is the worked example. PR flashback-cns#179 -- the right fix, with
226 lines of new tests -- merged at 04:15:43 on 2026-08-26. One second later the
job was `failed`, and its Trello card was left stranded in In progress because
the terminal handler never ran for a "failed" job. The work was perfect and the
record was wrong, which is the worst combination: nothing alerts, and the next
person reads the backlog as if the card was never done.

The tests below pin the fix from both sides. Losing the race must be a no-op --
and winning it must still work, or a guard that simply never advanced anything
would pass just as well.
"""

from unittest.mock import AsyncMock, MagicMock

from minions.config import Config
from minions.core.models import AgentRole, JobStatus, Task, TaskStatus
from minions.engine.dev import manage_dev_tasks


def _engine(db):
    engine = MagicMock()
    engine.db = db
    engine.config = Config.from_env()
    engine._spawn = MagicMock()
    engine._on_job_terminal = AsyncMock()
    engine._has_running_agent = AsyncMock(return_value=False)
    return engine


async def _dev_job(db, n: int = 1) -> tuple[str, list[str]]:
    job = await db.create_job("spec")
    for status in (JobStatus.SPEC_READY, JobStatus.TASKS_CREATED, JobStatus.DEV_IN_PROGRESS):
        await db.update_job_status(job.id, status)
    ids = []
    for i in range(n):
        task = await db.create_task(Task(job_id=job.id, title=f"t{i}", description="d", service=f"svc{i}", agent_role=AgentRole.BACKEND_ENGINEER))
        await db.update_task(task.id, status=TaskStatus.IN_PROGRESS)
        ids.append(task.id)
    return job.id, ids


class TestLosingTheRaceIsANoOp:
    async def test_a_second_advance_does_not_fail_a_merged_job(self, db):
        """The regression. Before the guard this raised InvalidTransitionError,
        which the graph node converted into a FAILED job."""
        job_id, task_ids = await _dev_job(db)
        await db.update_task(task_ids[0], status=TaskStatus.DONE, agent_role="code_reviewer")
        engine = _engine(db)

        await manage_dev_tasks(engine, await db.get_job(job_id))
        assert (await db.get_job(job_id)).status == JobStatus.MERGED

        # The second caller still holds the stale pre-merge job object -- that is
        # exactly the race, so pass the stale one, not a re-read.
        stale = await db.get_job(job_id)
        object.__setattr__(stale, "status", JobStatus.DEV_IN_PROGRESS)
        await manage_dev_tasks(engine, stale)

        assert (await db.get_job(job_id)).status == JobStatus.MERGED, "a second advance must not move a merged job"

    async def test_it_does_not_re_run_the_terminal_handler(self, db):
        """_on_job_terminal files the Trello card and sends the notification.
        Running it twice double-files and double-notifies."""
        job_id, task_ids = await _dev_job(db)
        await db.update_task(task_ids[0], status=TaskStatus.NO_WORK_NEEDED, agent_role="")
        engine = _engine(db)

        await manage_dev_tasks(engine, await db.get_job(job_id))
        stale = await db.get_job(job_id)
        object.__setattr__(stale, "status", JobStatus.DEV_IN_PROGRESS)
        await manage_dev_tasks(engine, stale)

        assert engine._on_job_terminal.await_count == 1, "the terminal handler must run exactly once"


class TestWinningTheRaceStillWorks:
    """Controls. A guard that returned early unconditionally would satisfy the
    tests above and break the pipeline completely."""

    async def test_the_first_advance_still_merges(self, db):
        job_id, task_ids = await _dev_job(db)
        await db.update_task(task_ids[0], status=TaskStatus.DONE, agent_role="code_reviewer")

        await manage_dev_tasks(_engine(db), await db.get_job(job_id))

        assert (await db.get_job(job_id)).status == JobStatus.MERGED

    async def test_a_genuine_all_failed_job_still_fails(self, db):
        job_id, task_ids = await _dev_job(db)
        await db.update_task(task_ids[0], status=TaskStatus.FAILED, agent_role="", error="boom")
        await db.update_task(task_ids[0], attempt=3)

        await manage_dev_tasks(_engine(db), await db.get_job(job_id))

        assert (await db.get_job(job_id)).status == JobStatus.FAILED

    async def test_a_no_work_job_still_records_no_work_needed(self, db):
        job_id, task_ids = await _dev_job(db)
        await db.update_task(task_ids[0], status=TaskStatus.NO_WORK_NEEDED, agent_role="")

        await manage_dev_tasks(_engine(db), await db.get_job(job_id))

        assert (await db.get_job(job_id)).status == JobStatus.NO_WORK_NEEDED
