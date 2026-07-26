"""A failed attempt must consume exactly one retry, and must actually retry.

Job 095146b8: the $8 agent cost ceiling correctly stopped a runaway engineer at
turn 55, and then the task burned all three attempts in TWELVE SECONDS without
ever launching a second agent:

    15:18:32 runner  ERROR   hit its $8.00 cost limit at turn 55 ($8.1837)
    15:18:35 dev     INFO    run_engineer:    incomplete subtasks, retrying (attempt 2)
    15:18:36 dev     INFO    orphan recovery: incomplete subtasks, retrying (attempt 2)
    15:18:42 dev     INFO    orphan recovery: incomplete subtasks, retrying (attempt 3)
    15:18:47 dev     WARNING no retries left, marking FAILED

Two independent races, both fixed here:

1. Two paths handle the SAME finished agent -- run_engineer's completion handler
   and the poll loop's orphan recovery -- and each increments `attempt` off its
   own stale read. Fixed by a single-owner guard: whoever wins moves the task
   out of IN_PROGRESS, and the loser sees that and returns.

2. Claiming a task sets IN_PROGRESS *before* run_engineer creates the new agent
   row, so for a moment the newest agent on record is the PREVIOUS attempt's
   finished one. Recovering against it consumes an attempt for work already
   being redone. Fixed by comparing the agent's start against the task's last
   update: an agent from THIS attempt is judged immediately, so a genuinely
   orphaned task still recovers on the next poll. The deferral is bounded, so
   an updated_at that moved for an unrelated reason cannot defer forever.

Both are the same underlying ambiguity that wedged revisions in job 0f90844d:
"nobody handled this" is indistinguishable from "someone else is handling it
right now" unless one of them claims it.
"""

import inspect
from datetime import UTC, datetime, timedelta

import pytest

from minions.core.models import TaskStatus
from minions.engine.dev import ORPHAN_GRACE_SECONDS, _agent_predates_current_attempt, _parse_ts


class _Task:
    def __init__(self, updated_at=None, status=TaskStatus.IN_PROGRESS):
        self.id = "t1"
        self.updated_at = updated_at
        self.status = status


def _ago(seconds):
    return datetime.now(UTC) - timedelta(seconds=seconds)


class _Agent:
    def __init__(self, started_at):
        self.id = "a1"
        self.started_at = started_at


class TestStaleAgentDetection:
    def test_an_agent_from_the_previous_attempt_defers_recovery(self):
        """The bug: task just re-claimed, newest agent row is still the old one."""
        task = _Task(_ago(2))
        agent = _Agent(_ago(300))

        assert _agent_predates_current_attempt(agent, task) is True

    def test_an_agent_from_this_attempt_is_judged_immediately(self):
        """A genuine orphan must still recover on the very next poll."""
        task = _Task(_ago(120))
        agent = _Agent(_ago(60))

        assert _agent_predates_current_attempt(agent, task) is False

    def test_deferral_is_bounded(self):
        """If updated_at moved for an unrelated reason, recovery must not be
        deferred forever -- a silent hang is worse than a late recovery."""
        task = _Task(_ago(ORPHAN_GRACE_SECONDS + 30))
        agent = _Agent(_ago(ORPHAN_GRACE_SECONDS + 90))

        assert _agent_predates_current_attempt(agent, task) is False

    def test_the_twelve_second_burn_window_is_covered(self):
        """Job 095146b8 consumed three attempts across 12s off one old agent."""
        agent = _Agent(_ago(400))
        for seconds in (1, 4, 6, 12):
            assert _agent_predates_current_attempt(agent, _Task(_ago(seconds))) is True, f"{seconds}s"

    @pytest.mark.parametrize("bad", [None, "", "not-a-timestamp"])
    def test_unreadable_timestamps_fall_through_to_judging(self, bad):
        assert _agent_predates_current_attempt(_Agent(bad), _Task(_ago(2))) is False
        assert _agent_predates_current_attempt(_Agent(_ago(300)), _Task(bad)) is False

    def test_naive_timestamps_are_treated_as_utc(self):
        """Postgres rows and the ISO strings in this codebase disagree on tzinfo."""
        naive_task = datetime.now(UTC).replace(tzinfo=None)

        assert _agent_predates_current_attempt(_Agent(_ago(300)), _Task(naive_task)) is True

    def test_iso_strings_and_datetimes_both_parse(self):
        now = datetime.now(UTC)

        assert _parse_ts(now) == now
        assert _parse_ts(now.isoformat()) is not None
        assert _parse_ts(None) is None


class TestSingleOwnerGuard:
    def test_completion_returns_early_when_another_path_won(self):
        source = inspect.getsource(__import__("minions.engine.dev", fromlist=["_try_complete_task"])._try_complete_task)

        assert "if task.status != TaskStatus.IN_PROGRESS:" in source
        assert "return" in source

    def test_the_guard_sits_after_the_re_read(self):
        """Guarding on a stale read would defeat the purpose."""
        source = inspect.getsource(__import__("minions.engine.dev", fromlist=["_try_complete_task"])._try_complete_task)

        reread = source.index("await engine.db.get_task(task.id)")
        guard = source.index("if task.status != TaskStatus.IN_PROGRESS:")

        assert reread < guard, "guard must run against the freshly re-read task"

    def test_orphan_recovery_checks_the_grace_period(self):
        from minions.engine import dev

        source = inspect.getsource(dev)

        assert "_agent_predates_current_attempt(latest_agent, task)" in source
