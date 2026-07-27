"""Two ways a revision round wasted money on job 793821e8.

That job spent $10.66 on a ten-line security fix and died at max_revisions,
having merged nothing. Reviewers were $8.68 of it — twelve agents across four
rounds. They were RIGHT every round, so the fix is not to review less; it is to
stop generating rounds that could not have gone differently.

**#36 — feedback was prose, so findings were optional.** Round 2 raised three
findings and the revision addressed one. Rounds 3 and 4 repeated the two it
skipped, verbatim and unanimously, and it never touched them. The feedback
itself loaded correctly every time (d629f22 works) — prose simply gives an agent
nothing to be accountable to. Findings are now enumerated and each one demands
an explicit FIXED or DECLINED.

**#37 — a revision that pushed nothing still bought a review round.** Revision 3
ran 38 turns, reported done, and committed nothing. The engine moved the task to
PR_OPEN anyway and three Opus reviewers re-derived their verdict against a
byte-identical diff: ~$2.4 to learn what was already known. The revision-scoped
dedup guard cannot catch this, because revision_count is bumped whether or not
the agent produced anything — the guard correctly sees a new revision of the
same code. An unchanged head SHA is the honest signal.
"""

import inspect
from unittest.mock import MagicMock, patch

from minions.engine.dev import _as_checklist, _pr_head_sha, run_engineer

REAL_REVIEW = """## Summary
CSV formula-injection hardening for the report download path.

## Findings
- **[warning]** app/utils/report_formatter.py:26 — `-`/`+` sanitization corrupts numeric
  values serialized as JSON strings.
- **[warning]** tests/test_report_csv.py:162 — `test_empty_report` was removed without an
  equivalent replacement.
- **[nit]** app/utils/report_formatter.py:58 — docstring wording.
"""


class TestFindingsBecomeAChecklist:
    def test_each_finding_is_numbered(self):
        out = _as_checklist([REAL_REVIEW])

        assert "1. [ ]" in out
        assert "2. [ ]" in out
        assert "3. [ ]" in out

    def test_the_count_is_stated_up_front(self):
        """An agent that knows there are three cannot quietly deliver one."""
        assert "3 finding(s)" in _as_checklist([REAL_REVIEW])

    def test_severity_and_location_survive(self):
        out = _as_checklist([REAL_REVIEW])

        assert "WARNING" in out
        assert "app/utils/report_formatter.py:26" in out

    def test_a_disposition_is_demanded_per_finding(self):
        out = _as_checklist([REAL_REVIEW])

        assert "FIX it" in out
        assert "DECLINE it" in out
        assert "FIXED or DECLINED" in out

    def test_declining_is_allowed(self):
        """A hard 'fix everything' invites the agent to fake compliance. An
        explicit, reasoned decline is a legitimate outcome; silence is not."""
        out = _as_checklist([REAL_REVIEW])

        assert "Declining with a reason is acceptable" in out
        assert "Silently skipping one is not" in out

    def test_restoring_deleted_work_is_named_as_a_fix(self):
        """The exact finding skipped three rounds running was 'you deleted these
        tests'. An agent can read that as a complaint rather than an action."""
        out = _as_checklist([REAL_REVIEW])

        assert "put it back" in out

    def test_the_full_review_text_is_still_included(self):
        """The checklist is a summary, not a replacement — the agent still needs
        the reviewer's reasoning to act well."""
        out = _as_checklist([REAL_REVIEW])

        assert "FULL REVIEW TEXT" in out
        assert "corrupts numeric" in out

    def test_multiple_reviews_are_pooled_into_one_list(self):
        out = _as_checklist([REAL_REVIEW, REAL_REVIEW])

        assert "6 finding(s)" in out

    def test_unparseable_prose_still_gets_strong_framing(self):
        """A persona that formats differently must not end up with WEAKER
        instructions than one that happens to match the regex."""
        out = _as_checklist(["Please fix the thing. It is wrong."])

        assert "EVERY point" in out
        assert "fixed it or are declining it" in out
        assert "Please fix the thing" in out

    def test_empty_bodies_do_not_crash(self):
        assert isinstance(_as_checklist([]), str)
        assert isinstance(_as_checklist(["", ""]), str)


class TestNoOpRevisionSkipsReview:
    def test_the_head_sha_is_captured_before_the_agent_runs(self):
        source = inspect.getsource(run_engineer)

        assert "sha_before_revision = await _pr_head_sha(task)" in source

    def test_an_unchanged_sha_skips_re_review(self):
        source = inspect.getsource(run_engineer)

        assert "sha_before_revision == sha_after" in source
        assert "skipping re-review" in source

    def test_it_falls_through_to_retry_or_fail(self):
        """A revision that achieved nothing belongs on the failure path, not in
        front of three more reviewers."""
        source = inspect.getsource(run_engineer)

        idx_guard = source.index("sha_before_revision == sha_after")
        idx_retry = source.index("_retry_or_fail", idx_guard)
        idx_propen = source.index("TaskStatus.PR_OPEN", idx_guard)
        assert idx_retry < idx_propen, "the no-op path must divert before the PR_OPEN transition"

    def test_an_unknown_sha_does_not_suppress_review(self):
        """FAIL-OPEN on purpose. '' means 'could not determine', and treating
        that as 'unchanged' would silently swallow a real revision's re-review —
        far worse than paying for one extra round."""
        source = inspect.getsource(run_engineer)

        assert "if sha_before_revision and sha_after and sha_before_revision == sha_after:" in source


class TestHeadShaLookup:
    async def test_a_non_github_url_yields_empty(self):
        task = MagicMock(pr_url="https://gitlab.com/x/y/-/merge_requests/3", mr_url=None)

        assert await _pr_head_sha(task) == ""

    async def test_a_missing_url_yields_empty(self):
        task = MagicMock(pr_url=None, mr_url=None)

        assert await _pr_head_sha(task) == ""

    async def test_it_returns_the_sha(self):
        task = MagicMock(pr_url="https://github.com/o/r/pull/83", mr_url=None)
        with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="abc123def\n", stderr="")):
            assert await _pr_head_sha(task) == "abc123def"

    async def test_a_provider_error_yields_empty_not_an_exception(self):
        task = MagicMock(pr_url="https://github.com/o/r/pull/83", mr_url=None)
        with patch("subprocess.run", return_value=MagicMock(returncode=1, stdout="", stderr="boom")):
            assert await _pr_head_sha(task) == ""

    async def test_a_raising_subprocess_yields_empty(self):
        task = MagicMock(pr_url="https://github.com/o/r/pull/83", mr_url=None)
        with patch("subprocess.run", side_effect=OSError("gh missing")):
            assert await _pr_head_sha(task) == ""


class TestFeedbackWiringUnchanged:
    """The checklist sits on top of the fetch that already worked — it must not
    replace it. get_review_feedback still prefers messages, still falls back to
    the PR, and still says so loudly when both fail."""

    def test_the_pr_fallback_still_runs(self):
        from minions.engine.dev import get_review_feedback

        source = inspect.getsource(get_review_feedback)
        assert "_fetch_pr_review_bodies(engine, task)" in source

    def test_total_failure_is_still_loud(self):
        from minions.engine.dev import get_review_feedback

        source = inspect.getsource(get_review_feedback)
        assert "FEEDBACK LOOKUP FAILED" in source

    def test_the_checklist_is_applied_to_pr_reviews(self):
        from minions.engine.dev import _fetch_pr_review_bodies

        source = inspect.getsource(_fetch_pr_review_bodies)
        assert "_as_checklist(bodies)" in source
