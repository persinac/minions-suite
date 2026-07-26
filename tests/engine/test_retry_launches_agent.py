"""A retry must actually launch an agent.

Retries were inert from the beginning. The poll loop claims a task
(PENDING -> IN_PROGRESS) *before* spawning run_engineer, so on a retry the task
is already IN_PROGRESS when run_engineer's `is_retry` branch unconditionally
writes IN_PROGRESS again. That is an illegal same-status transition, and the
handler returns — before the agent row is ever created.

The symptom looked like two different bugs:

* job 095146b8 recorded three attempts against ONE agent row, which read as a
  retry-accounting race (task #22 — a real bug, but not this one)
* job d1925a3f cycled pending -> in_progress -> failed -> pending every 60s
  (the ORPHAN_GRACE_SECONDS window) without spending a cent after its first
  agent died, because each "retry" aborted in milliseconds

Both are this: attempt 1 runs, every later attempt is a no-op. The retry budget
exists, is counted, is logged — and never buys a second attempt.

These tests assert on the guard rather than driving the whole engine, because
the defect was a missing conditional, not a control-flow shape.
"""

import inspect

import pytest

from minions.core.models import TaskStatus
from minions.engine.dev import run_engineer


class _Task:
    def __init__(self, status):
        self.id = "t1"
        self.status = status


def _claim_updates(current_status):
    """Mirror of the guard now in run_engineer's is_retry branch."""
    updates = {"branch_name": "feat-job-abc-thing"}
    current = _Task(current_status)
    if not current or current.status != TaskStatus.IN_PROGRESS:
        updates["status"] = TaskStatus.IN_PROGRESS
    return updates


class TestClaimGuard:
    def test_an_already_claimed_task_is_not_reclaimed(self):
        """The poll loop claimed it; writing IN_PROGRESS again is illegal and
        would abort the retry before the agent exists."""
        updates = _claim_updates(TaskStatus.IN_PROGRESS)

        assert "status" not in updates
        assert updates["branch_name"]

    @pytest.mark.parametrize("status", [TaskStatus.PENDING, TaskStatus.FAILED])
    def test_an_unclaimed_task_is_still_claimed(self, status):
        """run_engineer must remain able to claim a task nobody else has."""
        updates = _claim_updates(status)

        assert updates["status"] == TaskStatus.IN_PROGRESS

    def test_the_branch_name_is_written_either_way(self):
        """The branch name is the point of the write; losing it would leave the
        retry without somewhere to push."""
        for status in (TaskStatus.IN_PROGRESS, TaskStatus.PENDING):
            assert _claim_updates(status)["branch_name"] == "feat-job-abc-thing"


class TestWiring:
    def test_run_engineer_checks_before_claiming(self):
        source = inspect.getsource(run_engineer)

        assert 'updates = {"branch_name": branch_name}' in source
        assert "current.status != TaskStatus.IN_PROGRESS" in source

    def test_it_no_longer_writes_status_unconditionally(self):
        """The exact line that made every retry a no-op."""
        source = inspect.getsource(run_engineer)

        assert "update_task(task.id, branch_name=branch_name, status=TaskStatus.IN_PROGRESS)" not in source

    def test_the_early_return_is_still_there_for_real_failures(self):
        """A genuinely illegal transition must still abort rather than run an
        agent against a task in an unexpected state."""
        source = inspect.getsource(run_engineer)

        assert "except InvalidTransitionError" in source
