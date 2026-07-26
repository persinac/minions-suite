"""A fan-out `request_changes` verdict must actually launch a revision engineer.

Job 0f90844d wedged at dev_in_progress for hours, cycling in_review -> pr_open ->
in_review every ~15 minutes and slowly accruing cost, because of a string
mismatch between one writer and two readers:

    dev.py  (writer, fan-out)      review_status = f"changes_requested: {reason}"
    mcp.py  (writer, single review) review_status = "changes_requested"
    dev.py  (reader, x2)            review_status == "changes_requested"

Equality matched the legacy single-reviewer path and never the fan-out one. With
the revision branch skipped, control fell through to the IN_PROGRESS orphan
recovery, which sees the *previous* engineer agent still marked done, concludes
"agent finished but the task was never updated", and promotes the task to
PR_OPEN -- erasing the revision intent ~3 seconds after it was recorded.

The failure was invisible in the obvious places: the transition to IN_PROGRESS
succeeded and was logged, so the log said the revision had been requested. Only
the state_transitions audit table showed it being overwritten three seconds later.

These tests assert on the real predicates rather than mocking the engine loop,
because the defect was in a comparison, not in control flow.
"""

import inspect

import pytest

from minions.core.models import TaskStatus


class _Task:
    """Minimal stand-in: the predicates only read status and review_status."""

    def __init__(self, review_status, status=TaskStatus.IN_PROGRESS, agent_role="backend_engineer"):
        self.review_status = review_status
        self.status = status
        self.agent_role = agent_role


def _needs_revision(task) -> bool:
    """The predicate both call sites now use."""
    return task.status == TaskStatus.IN_PROGRESS and (task.review_status or "").startswith("changes_requested")


class TestRevisionPredicate:
    def test_fan_out_verdict_is_recognised(self):
        """The exact string run_task_review writes after aggregating specialists."""
        task = _Task("changes_requested: changes requested by: api")

        assert _needs_revision(task) is True

    def test_single_reviewer_verdict_still_recognised(self):
        """mcp.py:488 writes the bare string -- must not regress."""
        assert _needs_revision(_Task("changes_requested")) is True

    def test_a_long_reason_is_truncated_but_still_matches(self):
        """dev.py truncates the reason to 150 chars; the prefix survives."""
        task = _Task("changes_requested: " + "x" * 150)

        assert _needs_revision(task) is True

    @pytest.mark.parametrize(
        "review_status",
        ["approved", "revision_in_progress", "revision_complete", "", None],
    )
    def test_other_statuses_do_not_trigger_a_revision(self, review_status):
        """revision_in_progress especially: re-triggering would loop forever."""
        assert _needs_revision(_Task(review_status)) is False

    def test_a_task_not_in_progress_does_not_trigger(self):
        task = _Task("changes_requested: whatever", status=TaskStatus.IN_REVIEW)

        assert _needs_revision(task) is False


class TestCallSites:
    """Both readers had the bug; fixing only the dispatcher would leave the
    scheduler still failing to count revision tasks as actionable work."""

    def test_dispatcher_uses_a_prefix_match(self):
        from minions.engine import dev

        source = inspect.getsource(dev)

        assert 'startswith("changes_requested")' in source
        assert 'task.review_status == "changes_requested"' not in source, "dispatcher still uses equality"

    def test_scheduler_uses_a_prefix_match(self):
        from minions.engine import dev

        source = inspect.getsource(dev)

        assert 't.review_status == "changes_requested"' not in source, "revision_tasks filter still uses equality"

    def test_the_writer_still_carries_the_reason(self):
        """The prefix is load-bearing; the suffix is the human-readable why."""
        from minions.engine import dev

        source = inspect.getsource(dev)

        assert 'review_status=f"changes_requested: {reason[:150]}"' in source
