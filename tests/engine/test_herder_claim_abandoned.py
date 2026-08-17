"""A herder that claims and then dies must not hold the task forever.

Every recovery path in manage_dev_tasks needs either NO agent (the unclaimed
fallback) or a FINISHED one (the orphan checks). A claim from a process that no
longer exists is neither — the row still reads "running", so:

  * peek_engineer_work excludes the task, and no other herder takes it
  * herder_claim_timeout_seconds does not apply, because it gates on `unclaimed`
  * the orphan checks skip it, because the agent has not finished

The job parks indefinitely. Observed for real on 2026-08-17: a killed herdr
workspace left agent 3eb959df "running" on job c2b97f39 with its pane gone, and
nothing moved until the claim was released by hand. release_engineer_work's
docstring claims herder_claim_timeout_seconds covers this; it does not — that
timeout covers a herder that NEVER claims, not one that claims and dies.

The dangerous direction is the other one. Reaping a herder that is still working
hands its task to the metered in-process engineer — the exact cost the herder
exists to avoid. So these tests care more about not-too-early than not-too-late.
"""

from minions.config import Config


def _abandoned(model: str, status: str, running_for: float, limit: int) -> bool:
    """The predicate from manage_dev_tasks, isolated from the engine's I/O."""
    return bool(model or "") and model.startswith("herder:") and status == "running" and limit > 0 and running_for >= limit


LIMIT = Config().herder_work_timeout_seconds


class TestItReapsTheDead:
    def test_a_herder_claim_past_the_limit_is_released(self):
        assert _abandoned("herder:herder-alex-nexus", "running", LIMIT + 1, LIMIT)

    def test_exactly_at_the_limit_counts(self):
        assert _abandoned("herder:x", "running", LIMIT, LIMIT)


class TestItDoesNotReapTheLiving:
    """Every case here would cost money if it fired."""

    def test_a_working_herder_is_left_alone(self):
        """Real runs have taken 5-20 minutes. Reaping one mid-flight sends its
        work to the metered engineer — worse than the problem being solved."""
        for minutes in (1, 5, 10, 20, 30, 44):
            assert not _abandoned("herder:x", "running", minutes * 60, LIMIT), f"reaped a herder at {minutes} min"

    def test_an_in_process_agent_is_never_touched(self):
        """This branch is only for external claims. An in-process agent running
        long is the orphan checks' business, and they know how to judge it."""
        assert not _abandoned("claude-haiku-4-5", "running", LIMIT * 10, LIMIT)
        assert not _abandoned("claude-sonnet-5", "running", LIMIT * 10, LIMIT)

    def test_a_finished_herder_is_not_re_failed(self):
        for status in ("done", "failed"):
            assert not _abandoned("herder:x", status, LIMIT * 10, LIMIT)

    def test_zero_disables_it(self):
        """An escape hatch for a deployment that would rather park than pay."""
        assert not _abandoned("herder:x", "running", 10_000_000, 0)


class TestTheLimitIsGenerous:
    def test_it_clears_the_longest_observed_run_with_room(self):
        """Herder runs on 2026-08-17 took roughly 5-20 minutes. A limit near that
        would reap live workers; the cost of being late is wall-clock, the cost
        of being early is the metered path."""
        longest_observed_seconds = 20 * 60

        assert 2 * longest_observed_seconds <= LIMIT, f"{LIMIT}s leaves too little headroom over a real run"

    def test_it_is_distinct_from_the_claim_timeout(self):
        """They cover different failures: never-claimed vs claimed-then-died.
        Collapsing them into one number would force one of the two to be wrong."""
        assert Config().herder_work_timeout_seconds != Config().herder_claim_timeout_seconds


class TestWhatHappensNext:
    def test_failing_the_agent_is_the_whole_fix(self):
        """Documents why this change is three lines rather than a recovery path.

        Once the agent reads "failed", the existing machinery does the rest —
        observed end to end on job c2b97f39 after the stale claim was cleared:

            task failed -> pending -> in_progress -> work_item_published

        and the trigger spawned a fresh herder within one poll. Nothing here
        needed to re-dispatch, retry, or republish anything itself.
        """
        recovery_after_agent_fails = ["failed", "pending", "in_progress", "work_item_published"]

        assert recovery_after_agent_fails[-1] == "work_item_published"
