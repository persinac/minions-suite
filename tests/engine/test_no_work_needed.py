"""An engineer that finds the work already done must be able to say so.

`JobStatus.NO_WORK_NEEDED` existed from the beginning; tasks had no equivalent.
So an engineer that read the codebase and correctly concluded the requested
change was already present had no way to record it. Engineer tasks are retried
when they finish without a PR, and `("in_progress", "done")` is role-restricted
away from engineers — so the right answer could only present as an incomplete
one, and burned every attempt before failing the job.

Job 7b840e7f is the worked example. Its Trello card claimed wallet-api had no
real-DB test pattern; commit 8997ef8 had added one weeks earlier. The agent
verified that correctly, then — having no way to report it — invented a
docs-only change to justify the run. The pipeline recorded a failure for an
agent whose judgment was sound.

Three of the first four tech-debt cards checked on 2026-08-14 were likewise
already implemented, so this is the common case, not the exotic one.
"""

import pytest

from minions.core.models import AgentRole, JobStatus, Task, TaskStatus
from minions.core.state_transitions import (
    InvalidTransitionError,
    validate_task_transition,
)


class TestTransitions:
    def test_an_engineer_may_report_it_from_in_progress(self):
        """The whole point: engineers are the ones who discover this."""
        validate_task_transition("t1", "in_progress", "no_work_needed", agent_role="backend_engineer")

    def test_it_is_terminal(self):
        with pytest.raises(InvalidTransitionError):
            validate_task_transition("t1", "no_work_needed", "in_progress")

    def test_it_is_not_reachable_once_a_pr_exists(self):
        """A task with a PR plainly needed doing. Retreating to "nothing to do"
        from there would strand the open PR."""
        with pytest.raises(InvalidTransitionError):
            validate_task_transition("t1", "pr_open", "no_work_needed")

    def test_it_is_not_reachable_from_review(self):
        with pytest.raises(InvalidTransitionError):
            validate_task_transition("t1", "in_review", "no_work_needed")

    def test_a_database_engineer_may_report_it_too(self):
        """database_engineer uses its own transition map and would otherwise be
        silently excluded."""
        validate_task_transition("t1", "in_progress", "no_work_needed", agent_role="database_engineer")

    def test_engineers_still_cannot_mark_a_task_done(self):
        """Control. no_work_needed must not become a back door around review —
        it is separate from `done`, which stays reserved."""
        with pytest.raises(InvalidTransitionError):
            validate_task_transition("t1", "in_progress", "done", agent_role="backend_engineer")


async def _job_with_tasks(db, n: int = 1) -> tuple[str, list[str]]:
    job = await db.create_job("spec")
    for status in (JobStatus.SPEC_READY, JobStatus.TASKS_CREATED, JobStatus.DEV_IN_PROGRESS):
        await db.update_job_status(job.id, status)
    ids = []
    for i in range(n):
        task = await db.create_task(Task(job_id=job.id, title=f"t{i}", description="d", service=f"svc{i}", agent_role=AgentRole.BACKEND_ENGINEER))
        await db.update_task(task.id, status=TaskStatus.IN_PROGRESS)
        ids.append(task.id)
    return job.id, ids


class TestJobResolution:
    async def test_a_job_where_nothing_was_needed_is_not_a_failure(self, db, monkeypatch):
        """Before this, such a job fell through to "all dev tasks terminal but
        none merged" and was recorded FAILED — misfiling a correct outcome."""
        from minions.engine.dev import manage_dev_tasks

        job_id, task_ids = await _job_with_tasks(db, 1)
        await db.update_task(task_ids[0], status=TaskStatus.NO_WORK_NEEDED, agent_role="")

        engine = _engine(db, monkeypatch)
        await manage_dev_tasks(engine, await db.get_job(job_id))

        job = await db.get_job(job_id)
        assert job.status == JobStatus.NO_WORK_NEEDED, f"expected no_work_needed, got {job.status}"

    async def test_it_does_not_claim_a_merge(self, db, monkeypatch):
        """Advancing to MERGED would send a deploy monitor to watch a rollout
        that never happens — there is no PR and no change."""
        from minions.engine.dev import manage_dev_tasks

        job_id, task_ids = await _job_with_tasks(db, 1)
        await db.update_task(task_ids[0], status=TaskStatus.NO_WORK_NEEDED, agent_role="")

        engine = _engine(db, monkeypatch)
        await manage_dev_tasks(engine, await db.get_job(job_id))

        assert (await db.get_job(job_id)).status != JobStatus.MERGED

    async def test_one_task_finding_nothing_does_not_cancel_another_s_pr(self, db, monkeypatch):
        """A MIX must still merge. Otherwise a single already-done subtask would
        discard a sibling's real work."""
        from minions.engine.dev import manage_dev_tasks

        job_id, task_ids = await _job_with_tasks(db, 2)
        await db.update_task(task_ids[0], status=TaskStatus.NO_WORK_NEEDED, agent_role="")
        await db.update_task(task_ids[1], status=TaskStatus.DONE, agent_role="code_reviewer")

        engine = _engine(db, monkeypatch)
        await manage_dev_tasks(engine, await db.get_job(job_id))

        assert (await db.get_job(job_id)).status == JobStatus.MERGED

    async def test_a_real_failure_alongside_it_is_still_a_failure(self, db, monkeypatch):
        """no_work_needed must not launder a genuine failure into a clean job."""
        from minions.engine.dev import manage_dev_tasks

        job_id, task_ids = await _job_with_tasks(db, 2)
        await db.update_task(task_ids[0], status=TaskStatus.NO_WORK_NEEDED, agent_role="")
        await db.update_task(task_ids[1], status=TaskStatus.FAILED, agent_role="", error="boom")
        await db.update_task(task_ids[1], attempt=3)

        engine = _engine(db, monkeypatch)
        await manage_dev_tasks(engine, await db.get_job(job_id))

        assert (await db.get_job(job_id)).status == JobStatus.FAILED


def _engine(db, monkeypatch):
    from unittest.mock import AsyncMock, MagicMock

    from minions.config import Config

    engine = MagicMock()
    engine.db = db
    engine.config = Config.from_env()
    engine._spawn = MagicMock()
    engine._on_job_terminal = AsyncMock()
    return engine
