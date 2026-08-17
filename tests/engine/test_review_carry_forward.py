"""A specialist that approved does not re-read the same PR to say so again.

Every revision round used to re-run the whole panel regardless of who objected.
Job 52507587 is the receipt: `backend-architecture` approved in all three rounds
and `api` was re-run twice after approving. Twelve reviewer agents on one PR,
$2.364 — 98.5% of that job's cost, against $0.036 of orchestration and $0 for the
engineer, which the herder had already moved to a subscription.

The saving has one trap in it, and it is the reason these tests exist. Verdict
aggregation FAILS CLOSED: a specialty missing from the verdict dict counts as a
missing verdict, not an abstention. So skipping a specialist without seeding its
prior approval would turn every revision into an automatic request_changes —
spending MORE, not less, and looking like the reviewers had objected.
"""

import pytest

from minions.reviewers import APPROVE, DISCUSS, NOT_APPLICABLE, REQUEST_CHANGES, aggregate_verdicts


class TestAggregationSeesTheWholePanel:
    """These pin the property the carry-forward depends on."""

    def test_a_missing_specialty_is_not_an_approval(self):
        """The trap. If a carried approval were dropped instead of seeded, this
        is the verdict every revision would get."""
        verdict, _ = aggregate_verdicts({"api": APPROVE, "pythonista": None})

        assert verdict == REQUEST_CHANGES

    def test_carried_plus_fresh_approvals_approve(self):
        """The intended path: one specialist re-ran, the rest carried."""
        verdict, _ = aggregate_verdicts({"api": APPROVE, "backend-architecture": APPROVE, "pythonista": APPROVE})

        assert verdict == APPROVE

    def test_a_fresh_objection_still_wins_over_carried_approvals(self):
        """Carrying approvals must not be able to outvote a live objection."""
        verdict, _ = aggregate_verdicts({"api": APPROVE, "backend-architecture": APPROVE, "pythonista": REQUEST_CHANGES})

        assert verdict == REQUEST_CHANGES

    def test_discuss_still_blocks(self):
        verdict, _ = aggregate_verdicts({"api": APPROVE, "pythonista": DISCUSS})

        assert verdict == DISCUSS

    def test_not_applicable_abstains_rather_than_approving(self):
        """A specialist that found nothing in scope is not a vote to merge."""
        verdict, _ = aggregate_verdicts({"api": APPROVE, "dba": NOT_APPLICABLE})

        assert verdict == APPROVE


class TestWhoGetsCarried:
    """The selection arithmetic, isolated from the engine's I/O.

    Mirrors the comprehension in run_task_review: carried = prior approvals for
    the previous revision on this PR; to_run = selected minus carried.
    """

    @staticmethod
    def _select(selected, prior):
        carried = {s: APPROVE for s, v in prior.items() if v == APPROVE and s in selected}
        to_run = [s for s in selected if s not in carried]
        if not to_run:
            return {}, list(selected)
        return carried, to_run

    def test_approvers_are_skipped_and_objectors_re_run(self):
        selected = ["api", "backend-architecture", "pythonista"]
        prior = {"api": APPROVE, "backend-architecture": APPROVE, "pythonista": REQUEST_CHANGES}

        carried, to_run = self._select(selected, prior)

        assert sorted(carried) == ["api", "backend-architecture"]
        assert to_run == ["pythonista"]

    def test_a_newly_woken_specialist_runs_even_though_it_never_voted(self):
        """`specialists` is recomputed from the NEW diff, so a revision that adds
        SQL wakes the dba — which has no prior verdict and must not be skipped."""
        selected = ["api", "backend-architecture", "pythonista", "dba"]
        prior = {"api": APPROVE, "backend-architecture": APPROVE, "pythonista": REQUEST_CHANGES}

        carried, to_run = self._select(selected, prior)

        assert "dba" in to_run
        assert "dba" not in carried, "a specialist with no prior verdict must never be carried"

    def test_a_prior_approver_no_longer_selected_is_not_resurrected(self):
        """If the revision no longer touches Python, pythonista should not appear
        in the verdict dict at all — carrying it would vote on a domain the new
        diff does not have."""
        selected = ["api", "backend-architecture"]
        prior = {"api": APPROVE, "backend-architecture": REQUEST_CHANGES, "pythonista": APPROVE}

        carried, to_run = self._select(selected, prior)

        assert "pythonista" not in carried
        assert "pythonista" not in to_run

    def test_a_non_approving_prior_verdict_is_never_carried(self):
        for verdict in (REQUEST_CHANGES, DISCUSS, NOT_APPLICABLE, None, ""):
            carried, to_run = self._select(["api", "pythonista"], {"api": verdict, "pythonista": REQUEST_CHANGES})

            assert "api" not in carried, f"{verdict!r} must not count as an approval"
            assert "api" in to_run

    def test_everyone_approving_falls_back_to_the_full_panel(self):
        """Unreachable in practice — a revision implies someone objected. But
        approving on nothing but stale verdicts would merge a diff no reviewer
        has read, so the fallback runs everyone rather than trusting them."""
        selected = ["api", "backend-architecture"]
        prior = {"api": APPROVE, "backend-architecture": APPROVE}

        carried, to_run = self._select(selected, prior)

        assert carried == {}
        assert to_run == selected


class TestTheSavingIsReal:
    def test_job_52507587_would_have_run_seven_agents_not_twelve(self):
        """Reconstruction from the recorded verdicts.

        rev=0  backend-architecture approve, api approve, dba -, pythonista -
        rev=1  dba approve, backend-architecture approve, pythonista -, api -
        rev=2  all four approve
        """
        panel = ["api", "backend-architecture", "dba", "pythonista"]
        rounds = [
            {},  # round 0: nothing prior, full panel
            {"backend-architecture": APPROVE, "api": APPROVE, "dba": None, "pythonista": None},
            {"dba": APPROVE, "backend-architecture": APPROVE, "pythonista": None, "api": None},
        ]

        total = 0
        for prior in rounds:
            _, to_run = TestWhoGetsCarried._select(panel, prior)
            total += len(to_run)

        assert total == 8, f"expected 8 reviewer runs, got {total}"
        assert total < 12, "must be cheaper than re-running the full panel every round"


@pytest.mark.parametrize("max_revisions_default", [2])
def test_max_revisions_lowered(max_revisions_default):
    """Each round re-pays the panel, so this multiplies the dominant cost.

    Round three on job 52507587 returned four unanimous approvals — it bought
    confirmation, not correction.
    """
    from minions.config import Config

    assert Config().max_revisions == max_revisions_default
