"""A retried external work item is unclaimed too, and must be treated as such.

External dispatch publishes a work item and creates NO agent row, and the
absence of that row is what tells the poll loop "nobody owns this yet, wait for
a herder, then run it in-process". The test for it was `latest_agent is None`.

That holds on the first attempt only. A retry re-publishes the item, but the
PREVIOUS attempt's agent row is still the newest one on record — so
`latest_agent is None` is False, the fallback branch is skipped, and recovery
falls through to judging the retry against an agent that finished before the
retry began. _agent_predates_current_attempt defers that, but only for
ORPHAN_GRACE_SECONDS; once the grace lapses the stale agent is judged anyway and
its already-known-incomplete subtasks fail the retry on the spot.

Job 7b840e7f: attempts 2 and 3 died 61s and 65s after being published, neither
having launched an agent, and the job failed with "max attempts reached" having
genuinely run exactly one.
"""

from datetime import UTC, datetime, timedelta

from minions.core.models import Task, TaskStatus
from minions.engine.dev import ORPHAN_GRACE_SECONDS, _agent_predates_current_attempt


class _Agent:
    def __init__(self, started_at: str, status: str = "done"):
        self.id = "a1"
        self.started_at = started_at
        self.status = status


def _task_updated(seconds_ago: float) -> Task:
    return Task(
        job_id="j1",
        title="t",
        description="d",
        service="svc",
        agent_role="backend_engineer",
        status=TaskStatus.IN_PROGRESS,
        updated_at=(datetime.now(UTC) - timedelta(seconds=seconds_ago)).isoformat(),
    )


class TestOwnershipIsTimeless:
    def test_a_stale_agent_is_still_stale_after_the_grace_window(self):
        """The bug, stated directly.

        The agent started before the current attempt, so it is not this
        attempt's agent. That fact does not expire — but the bounded form
        reported False once 60s had passed, which is what let a retry be judged
        against its predecessor.
        """
        task = _task_updated(ORPHAN_GRACE_SECONDS + 30)
        older = (datetime.now(UTC) - timedelta(seconds=ORPHAN_GRACE_SECONDS + 120)).isoformat()

        assert _agent_predates_current_attempt(_Agent(older), task, unbounded=True) is True, (
            "an agent that started before the current attempt is not this attempt's agent, no matter how long ago the attempt began"
        )

    def test_the_bounded_form_still_expires(self):
        """Deliberate asymmetry. When the question is "should recovery defer to
        an agent that may be about to appear", deferring forever would strand a
        genuinely orphaned task."""
        task = _task_updated(ORPHAN_GRACE_SECONDS + 30)
        older = (datetime.now(UTC) - timedelta(seconds=ORPHAN_GRACE_SECONDS + 120)).isoformat()

        assert _agent_predates_current_attempt(_Agent(older), task) is False

    def test_both_forms_agree_inside_the_grace_window(self):
        task = _task_updated(5)
        older = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()

        assert _agent_predates_current_attempt(_Agent(older), task) is True
        assert _agent_predates_current_attempt(_Agent(older), task, unbounded=True) is True

    def test_this_attempt_s_own_agent_is_never_stale(self):
        """An agent started at or after the attempt belongs to it and must be
        judged normally, or a genuinely orphaned task never recovers."""
        task = _task_updated(60)
        newer = datetime.now(UTC).isoformat()

        assert _agent_predates_current_attempt(_Agent(newer), task, unbounded=True) is False

    def test_unreadable_timestamps_fall_through_to_judging(self):
        """Unchanged: a missing timestamp must not make a task permanently
        un-recoverable by looking eternally unclaimed."""
        task = _task_updated(60)

        assert _agent_predates_current_attempt(_Agent(None), task, unbounded=True) is False


class TestTheFallbackCoversRetries:
    def test_the_unclaimed_check_is_not_a_bare_none_test(self):
        """`latest_agent is None` alone cannot see a retried work item, because
        the previous attempt's agent row outlives the attempt."""
        import inspect

        from minions.engine.dev import manage_dev_tasks

        source = inspect.getsource(manage_dev_tasks)

        assert "unbounded=True" in source, (
            "the unclaimed-work-item check must ask whether an agent exists for the CURRENT attempt, not whether any agent row exists at all"
        )
        assert "herder_claim_timeout_seconds" in source
        assert "force_in_process=True" in source
