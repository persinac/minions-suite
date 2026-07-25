"""The Trello poller must not move a card it did not actually start work on.

_launch_job moved the card to "in progress" and only then called create_job —
which was being handed a Job where it takes a spec string, the same signature bug
as submit_spec. Job creation raised, the poll loop's `except Exception` swallowed
it, and the next cycle drained the next batch.

Observed on the live board: on-deck emptied to 0 cards, 24 moved to "in progress"
carrying the minion label, with no job, no agent and no spend behind any of them.
From the board it looked like 24 tickets were being worked. Nothing was.

max_concurrent_jobs did not throttle it either: the slot count is derived from
active jobs, and no job ever got created, so there were always free slots.
"""

import pytest

from minions.config import Config


class TestLaunchOrder:
    def test_the_job_is_created_before_the_card_moves(self):
        """Ordering is the whole fix — a failed create must leave the card put."""
        import inspect

        from minions.providers.trello import TrelloPoller

        source = inspect.getsource(TrelloPoller._launch_job)
        create = source.index("create_job")
        move = source.index("_move_card")

        assert create < move, "create_job must precede _move_card, or a failure strands the card"

    def test_create_job_is_passed_a_spec_string(self):
        """create_job(spec: str, ...) builds the Job itself."""
        import inspect

        from minions.providers.trello import TrelloPoller

        source = inspect.getsource(TrelloPoller._launch_job)
        assert "create_job(spec_text" in source
        assert "create_job(job)" not in source

    def test_no_caller_anywhere_passes_a_job_object(self):
        """Three call sites had this bug; fixing one at a time missed two."""
        from pathlib import Path

        root = Path(__file__).resolve().parents[2] / "minions"
        offenders = [
            str(path.relative_to(root))
            for path in root.rglob("*.py")
            if "create_job(job)" in path.read_text(encoding="utf-8", errors="replace")
        ]

        assert not offenders, f"create_job takes a spec string, not a Job: {offenders}"


class TestConcurrency:
    def test_one_job_at_a_time(self):
        """Concurrency multiplies spend directly: the $25 ceiling is per job."""
        assert Config.from_env().max_concurrent_jobs == 1

    def test_the_slot_calculation_cannot_go_unbounded(self):
        """Slots derive from active jobs; the cap must bound the per-poll batch."""
        import inspect

        from minions.providers.trello import TrelloPoller

        source = inspect.getsource(TrelloPoller._poll)
        assert "max_concurrent_jobs" in source
        assert "cards[:slots]" in source, "the poll batch must be sliced by available slots"
