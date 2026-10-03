"""A dead herder's claim must not wedge a task that is waiting for its revision.

Job a9f2b36e (management-dashboard#96) stopped the whole line for seven days:

    10:37  herder claims task f014240d (agent c6cd5a94, model herder:...)
    10:48  herder pushes, calls report_pr — but never complete_engineer_work
    10:59  reviewer requests changes -> task IN_PROGRESS, review_status=changes_requested
    11:22  the herder's pane is reaped; its agent row still reads "running"

From then on every poll took the changes_requested branch of manage_dev_tasks,
saw a "running" agent, and hit `continue`. The abandoned-herder guard that would
have released the claim after herder_work_timeout_seconds lives in the OTHER
IN_PROGRESS branch, so it was never reached. No revision was dispatched, the
job sat at dev_in_progress, and with max_concurrent_jobs=1 no other card was
picked up either. Released by hand on 2026-10-03.

The guard was right; it was just unreachable from the one state where a herder
is most likely to have finished and walked away — after report_pr.
"""

from datetime import UTC, datetime, timedelta

from minions.core.models import Agent, AgentRole, JobStatus, Task, TaskStatus
from minions.engine.dev import manage_dev_tasks
from tests.engine.test_dev import _mock_engine

WORK_TIMEOUT = 2700


async def _age_agent(db, agent_id: str, seconds: float):
    stamp = (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()
    await db.update_agent(agent_id, started_at=stamp)


async def _job_with_engineer_task(db):
    job = await db.create_job("spec")
    for s in (JobStatus.SPEC_READY, JobStatus.TASKS_CREATED, JobStatus.DEV_IN_PROGRESS):
        await db.update_job_status(job.id, s)
    task = await db.create_task(
        Task(job_id=job.id, title="t", description="d", service="management-dashboard", agent_role=AgentRole.FRONTEND_ENGINEER)
    )
    await db.update_task(task.id, status=TaskStatus.IN_PROGRESS)
    return job, task


async def _awaiting_revision(db):
    """The exact shape job a9f2b36e was left in: reviewed, changes requested,
    back to IN_PROGRESS, revision not yet dispatched."""
    job, task = await _job_with_engineer_task(db)
    await db.update_task(task.id, pr_url="https://github.com/o/r/pull/96", pr_number=96, branch_name="feat-x")
    await db.update_task(task.id, status=TaskStatus.PR_OPEN)
    await db.update_task(task.id, status=TaskStatus.IN_REVIEW)
    await db.update_task(task.id, review_status="changes_requested: changes requested by: frontend")
    await db.update_task(task.id, status=TaskStatus.IN_PROGRESS)
    return job, await db.get_task(task.id)


async def _claim(db, job, task, *, model: str, age_seconds: float) -> Agent:
    agent = await db.create_agent(Agent(job_id=job.id, role=AgentRole.FRONTEND_ENGINEER, task_id=task.id, model=model, status="running"))
    await _age_agent(db, agent.id, age_seconds)
    return agent


def _engine(db):
    engine = _mock_engine(db)
    engine.config.herder_work_timeout_seconds = WORK_TIMEOUT
    return engine


def _spawned_names(engine) -> list[str]:
    names = []
    for call in engine._spawn.call_args_list:
        coro = call.args[0] if call.args else None
        if coro is not None and hasattr(coro, "close"):
            coro.close()
        names.append(call.kwargs.get("name", ""))
    return names


class TestADeadHerderOnARevisionIsReleased:
    async def test_the_claim_is_marked_failed(self, db):
        job, task = await _awaiting_revision(db)
        agent = await _claim(db, job, task, model="herder:herder-w4D-p1", age_seconds=7 * 24 * 3600)
        engine = _engine(db)

        await manage_dev_tasks(engine, await db.get_job(job.id))
        _spawned_names(engine)

        assert (await db.get_agent(agent.id)).status == "failed"

    async def test_the_revision_is_dispatched(self, db):
        """Releasing the claim is only half of it — the point is that the job
        moves. The revision branch must go on to spawn the revision engineer in
        the same poll, not leave it for a poll that never comes."""
        job, task = await _awaiting_revision(db)
        await _claim(db, job, task, model="herder:herder-w4D-p1", age_seconds=7 * 24 * 3600)
        engine = _engine(db)

        await manage_dev_tasks(engine, await db.get_job(job.id))

        assert f"eng-rev-{task.id[:8]}" in _spawned_names(engine)
        after = await db.get_task(task.id)
        assert after.review_status == "revision_in_progress"
        assert after.revision_count == 1

    async def test_the_release_is_recorded(self, db):
        job, task = await _awaiting_revision(db)
        await _claim(db, job, task, model="herder:gone", age_seconds=WORK_TIMEOUT + 60)
        engine = _engine(db)

        await manage_dev_tasks(engine, await db.get_job(job.id))
        _spawned_names(engine)

        assert [e for e in await db.get_events(job.id) if e["event_type"] == "herder_claim_abandoned"]


class TestALiveWorkerOnARevisionIsLeftAlone:
    """Reaping a working herder pushes its task onto the metered path, and here
    it would also start a second engineer on a branch the first is still
    pushing to. On a9f2b36e the herder pushed its own fix 18 minutes after the
    verdict — the claim was alive and working for that whole stretch."""

    async def test_a_herder_inside_the_limit_is_not_touched(self, db):
        job, task = await _awaiting_revision(db)
        agent = await _claim(db, job, task, model="herder:working", age_seconds=20 * 60)
        engine = _engine(db)

        await manage_dev_tasks(engine, await db.get_job(job.id))

        assert (await db.get_agent(agent.id)).status == "running"
        assert _spawned_names(engine) == []

    async def test_an_in_process_agent_is_never_reaped_as_a_herder(self, db):
        """The `herder:` prefix is the only thing that distinguishes them."""
        job, task = await _awaiting_revision(db)
        agent = await _claim(db, job, task, model="claude-sonnet-5", age_seconds=7 * 24 * 3600)
        engine = _engine(db)

        await manage_dev_tasks(engine, await db.get_job(job.id))

        assert (await db.get_agent(agent.id)).status == "running"
        assert _spawned_names(engine) == []

    async def test_zero_still_disables_it(self, db):
        job, task = await _awaiting_revision(db)
        agent = await _claim(db, job, task, model="herder:gone", age_seconds=7 * 24 * 3600)
        engine = _engine(db)
        engine.config.herder_work_timeout_seconds = 0

        await manage_dev_tasks(engine, await db.get_job(job.id))

        assert (await db.get_agent(agent.id)).status == "running"
        assert _spawned_names(engine) == []


class TestThePlainBranchStillReleases:
    """The original guard, now driven through manage_dev_tasks rather than a
    copy of its predicate — so moving it into a helper cannot silently drop it."""

    async def test_a_dead_herder_on_a_first_attempt_is_released(self, db):
        job, task = await _job_with_engineer_task(db)
        agent = await _claim(db, job, task, model="herder:gone", age_seconds=WORK_TIMEOUT + 60)
        engine = _engine(db)

        await manage_dev_tasks(engine, await db.get_job(job.id))
        _spawned_names(engine)

        assert (await db.get_agent(agent.id)).status == "failed"
        assert [e for e in await db.get_events(job.id) if e["event_type"] == "herder_claim_abandoned"]
