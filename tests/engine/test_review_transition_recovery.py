"""An interrupted review verdict must not wedge a task forever.

Recording "changes requested" and moving the task to IN_PROGRESS are two
separate writes. Job 33c89d9b lost the second one:

    10:39:25  review_aggregated  verdict=request_changes
    10:39:28  new pod started

The first write committed, the pod died, the second never ran. What remained —
IN_REVIEW carrying a changes_requested review_status — is a state NOTHING acts
on. The revision dispatcher matches IN_PROGRESS; the stuck-reviewer recovery
wants a failed agent; claim_engineer_work only offers IN_PROGRESS. The job
wedged permanently while the arbiter logged an anomaly every 30 seconds that its
remediation could not fix.

The shutdown drain does not help here. It waits for in-flight AGENTS, and this is
engine bookkeeping between two database writes with no agent involved — so a
hard crash, an eviction or an OOM reproduces it just as well as a rollout. That
is why the fix is recovery rather than write-ordering: ordering cannot survive a
process that stops between any two statements.
"""

import inspect

import pytest

from minions.core.models import AgentRole, JobStatus, Task, TaskStatus
from minions.engine.dev import manage_dev_tasks
from tests.engine.test_dev import _mock_engine


async def _task_in_state(db, status: TaskStatus, review_status: str | None):
    job = await db.create_job("spec")
    for s in (JobStatus.SPEC_READY, JobStatus.TASKS_CREATED, JobStatus.DEV_IN_PROGRESS):
        await db.update_job_status(job.id, s)
    task = await db.create_task(
        Task(job_id=job.id, title="t", description="d", service="api", agent_role=AgentRole.BACKEND_ENGINEER)
    )
    await db.update_task(task.id, status=TaskStatus.IN_PROGRESS)
    await db.update_task(task.id, pr_url="https://github.com/o/r/pull/1", pr_number=1, branch_name="feat/x")
    await db.update_task(task.id, status=TaskStatus.PR_OPEN)
    await db.update_task(task.id, status=TaskStatus.IN_REVIEW)
    if review_status is not None:
        await db.update_task(task.id, review_status=review_status)
    if status != TaskStatus.IN_REVIEW:
        await db.update_task(task.id, status=status)
    return job, await db.get_task(task.id)


class TestInterruptedTransitionIsCompleted:
    async def test_a_wedged_task_is_moved_to_in_progress(self, db, sample_job):
        job, task = await _task_in_state(db, TaskStatus.IN_REVIEW, "changes_requested: changes requested by: api")
        engine = _mock_engine(db)

        await manage_dev_tasks(engine, await db.get_job(job.id))

        assert (await db.get_task(task.id)).status == TaskStatus.IN_PROGRESS

    async def test_the_recovery_is_recorded(self, db):
        """A silent self-heal hides how often this happens."""
        job, _ = await _task_in_state(db, TaskStatus.IN_REVIEW, "changes_requested: x")
        engine = _mock_engine(db)

        await manage_dev_tasks(engine, await db.get_job(job.id))

        events = [e for e in await db.get_events(job.id) if e.get("event_type") == "review_transition_recovered"]
        assert events, "recovering from a wedged state must leave a trace"

    async def test_the_review_status_survives(self, db):
        """It is the revision's only instruction — losing it would produce a
        revision agent with nothing to act on."""
        job, task = await _task_in_state(db, TaskStatus.IN_REVIEW, "changes_requested: fix the thing")
        engine = _mock_engine(db)

        await manage_dev_tasks(engine, await db.get_job(job.id))

        assert "fix the thing" in (await db.get_task(task.id)).review_status


class TestNormalReviewIsUntouched:
    async def test_a_task_under_active_review_is_left_alone(self, db):
        """No verdict yet means reviewers are still working. Yanking it to
        IN_PROGRESS would abandon a review already being paid for."""
        job, task = await _task_in_state(db, TaskStatus.IN_REVIEW, None)
        engine = _mock_engine(db)

        await manage_dev_tasks(engine, await db.get_job(job.id))

        assert (await db.get_task(task.id)).status == TaskStatus.IN_REVIEW

    @pytest.mark.parametrize("review_status", ["revision_complete", "approved", "revision_in_progress"])
    async def test_other_review_statuses_do_not_trigger_it(self, db, review_status):
        job, task = await _task_in_state(db, TaskStatus.IN_REVIEW, review_status)
        engine = _mock_engine(db)

        await manage_dev_tasks(engine, await db.get_job(job.id))

        assert (await db.get_task(task.id)).status == TaskStatus.IN_REVIEW


class TestGuardShape:
    def test_it_matches_on_the_prefix_not_equality(self):
        """The stored value carries the reason —
        "changes_requested: changes requested by: api, backend-architecture" —
        so an equality check would never fire."""
        source = inspect.getsource(manage_dev_tasks)

        assert '(task.review_status or "").startswith("changes_requested")' in source

    def test_it_continues_rather_than_falling_through(self):
        """Falling through would run the stuck-reviewer recovery against a task
        that has just been moved out of IN_REVIEW."""
        source = inspect.getsource(manage_dev_tasks)

        idx = source.index("review_transition_recovered")
        assert "continue" in source[idx : idx + 400]
