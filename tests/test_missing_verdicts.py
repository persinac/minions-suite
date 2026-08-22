"""Telling "a reviewer objected" from "a reviewer did not answer".

aggregate_verdicts fails closed on an absent verdict, which is correct: silence
must never count as assent. But it returns the same REQUEST_CHANGES for both
cases, and they want opposite handling — an objection needs a revision, a
silence needs that reviewer run again.

Collapsing them has cost two merges:

* job 33c89d9b — api and backend-architecture both APPROVED, pythonista returned
  nothing. The forced revision round then came back with the opposite verdict on
  byte-identical code, turning a mergeable PR into a blocked one.
* job 2b63f1b6 — two approvals, zero findings, and a herder was asked to revise
  a PR nobody had objected to. Cost $0.34 of the job's $0.82.

missing_verdicts() is the discriminator. It is deliberately conservative:
whenever anything actually objects it returns nothing, so a caller acting on it
can only ever short-circuit the no-objection case and fail-closed is untouched.

Wired into run_task_review since 0.8.43: when it names silent reviewers, they
are re-run once (bounded, budget-checked) before aggregation, which still fails
closed if the silence persists. tests/engine/test_review_fanout.py
(TestSilenceIsRerunNotRevised) pins the wiring; this file pins the discriminator.
"""

import pytest

from minions.reviewers import aggregate_verdicts, missing_verdicts


class TestSilenceIsIdentified:
    def test_one_silent_reviewer_among_approvals(self):
        assert missing_verdicts({"api": "approve", "backend": "approve", "pythonista": None}) == ["pythonista"]

    def test_several_silent_reviewers_are_all_named(self):
        assert missing_verdicts({"api": None, "backend": "approve", "dba": None}) == ["api", "dba"]

    def test_an_unparseable_verdict_counts_as_silence(self):
        """A garbled answer is not an answer. normalise_verdict returns None for
        both, and the remedy is the same: run it again."""
        assert missing_verdicts({"api": "approve", "backend": "banana"}) == ["backend"]

    def test_the_result_is_sorted(self):
        """Stable ordering keeps log lines and events diffable."""
        assert missing_verdicts({"z": None, "a": None, "m": None}) == ["a", "m", "z"]


class TestARealObjectionSuppressesIt:
    """The safety property. Whenever anything objects, this returns nothing, so
    acting on it can never skip a revision that was genuinely requested."""

    def test_an_objection_alongside_silence_returns_nothing(self):
        assert missing_verdicts({"api": "request_changes", "backend": None}) == []

    def test_an_objection_alongside_approvals_returns_nothing(self):
        assert missing_verdicts({"api": "request_changes", "backend": "approve"}) == []

    def test_everyone_objecting_returns_nothing(self):
        assert missing_verdicts({"api": "request_changes", "backend": "request_changes"}) == []


class TestNothingToDo:
    def test_all_answered_returns_nothing(self):
        assert missing_verdicts({"api": "approve", "backend": "approve"}) == []

    def test_empty_returns_nothing(self):
        """aggregate_verdicts already blocks on "no reviewers ran"; there is no
        specialist to re-run, so this must not claim otherwise."""
        assert missing_verdicts({}) == []

    def test_a_discuss_verdict_is_an_answer(self):
        assert missing_verdicts({"api": "discuss", "backend": "approve"}) == []

    def test_a_not_applicable_verdict_is_an_answer(self):
        """N/A means the specialist looked and had no jurisdiction. That is a
        real reply, not silence — re-running it would just produce N/A again."""
        assert missing_verdicts({"api": "n/a", "backend": "approve"}) == []


class TestAgreesWithTheAggregator:
    """The two must not disagree about what blocked, or the caller would re-run
    reviewers on a PR that a revision should have handled."""

    @pytest.mark.parametrize(
        "verdicts",
        [
            {"api": "approve", "backend": None},
            {"api": "request_changes", "backend": None},
            {"api": "approve", "backend": "approve"},
            {"api": "request_changes", "backend": "approve"},
        ],
    )
    def test_silence_is_only_reported_when_the_aggregate_blocks(self, verdicts):
        verdict, _ = aggregate_verdicts(verdicts)
        if missing_verdicts(verdicts):
            assert verdict == "request_changes"

    def test_the_two_merges_this_was_built_for(self):
        """33c89d9b and 2b63f1b6: two approvals plus one silent reviewer. Both
        were blocked, neither had an objection."""
        verdicts = {"api": "approve", "backend-architecture": "approve", "pythonista": None}

        assert aggregate_verdicts(verdicts)[0] == "request_changes"
        assert missing_verdicts(verdicts) == ["pythonista"]
