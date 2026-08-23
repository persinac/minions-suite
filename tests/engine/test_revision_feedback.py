"""A revision agent must be told what the reviewers actually said.

get_review_feedback built its context from the internal messages table. The
specialist fan-out posts findings as GitHub PR reviews and writes nothing there,
so the lookup came up empty every time and every revision agent received a
placeholder: "the reviewer requested changes but no specific feedback was found
in messages."

Job 263b8b3e: three reviewers unanimously blocked PR #81 with file-and-line
citations. The revision engineer then ran to completion — 627k input tokens,
status done — and committed nothing, because it was told to revise without being
told what was wrong. The task returned to in_review, the dedup guard correctly
refused to re-review an unchanged PR, and the job idled with a green, blocked
PR. Every layer reported success.

Now the PR reviews are read from the provider when the message log is empty,
and a total lookup failure says so explicitly instead of reading like "the
reviewer had no comments" — an agent with no feedback cannot succeed and should
not spend a full budget discovering that.
"""

import inspect

from minions.engine.dev import _fetch_pr_review_bodies, get_review_feedback


class TestFallbackWiring:
    def test_messages_are_still_preferred(self):
        source = inspect.getsource(get_review_feedback)

        assert "get_messages(job_id)" in source
        prefer = source.index("feedback_parts")
        fallback = source.index("_fetch_pr_review_bodies")
        assert prefer < fallback, "the message log is consulted first"

    def test_it_falls_back_to_the_pull_request(self):
        source = inspect.getsource(get_review_feedback)

        assert "_fetch_pr_review_bodies(engine, task)" in source

    def test_total_failure_is_loud_not_silent(self):
        """The old string read like a normal outcome. An agent that cannot see
        the feedback must be told the lookup FAILED, and told not to guess."""
        source = inspect.getsource(get_review_feedback)

        assert "FEEDBACK LOOKUP FAILED" in source
        assert "Do not guess" in source
        assert "no specific feedback was found in messages." not in source


class TestReviewFetching:
    def test_blocking_reviews_lead_but_approvals_are_not_dropped(self):
        """`blocking or reviews` discarded every non-blocking body the moment
        anyone blocked — and an approving reviewer's body regularly carries
        real findings ("LGTM, but…"). Measured cost: a checklist of 2 items
        against a review text of 5. Blocking bodies still come first."""
        source = inspect.getsource(_fetch_pr_review_bodies)

        assert 'r.get("state") == "CHANGES_REQUESTED"' in source
        assert "chosen = blocking + others" in source
        assert "chosen = blocking or reviews" not in source

    def test_inline_comments_are_read_too(self):
        """The personas are told their findings land as inline comments; the
        old code read only the summary-bodies endpoint, so the most precise
        feedback (file:line) never reached a revision."""
        source = inspect.getsource(_fetch_pr_review_bodies)

        assert "/comments" in source
        assert "original_line" in source, "outdated-diff comments keep their original line"

    def test_inline_failure_does_not_cost_the_review_bodies(self):
        source = inspect.getsource(_fetch_pr_review_bodies)

        reviews_fail = source.index("Could not fetch PR reviews")
        inline_fetch = source.index("/comments")
        assert reviews_fail < inline_fetch, "reviews are fetched (and can fail loudly) before inline comments"
        assert source.count("Could not fetch PR inline comments") >= 2, "both inline failure modes are logged, neither returns early"

    def test_a_non_github_url_yields_nothing_rather_than_raising(self):
        source = inspect.getsource(_fetch_pr_review_bodies)

        assert 'github\\.com' in source or "github" in source
        assert 'return ""' in source

    def test_provider_failure_is_not_mistaken_for_no_comments(self):
        """Returning "" on error is only safe because the caller distinguishes
        it from feedback-found and emits the loud failure string."""
        source = inspect.getsource(_fetch_pr_review_bodies)

        assert "except Exception" in source
        assert "logger.warning" in source

    def test_it_reads_the_reviews_endpoint(self):
        source = inspect.getsource(_fetch_pr_review_bodies)

        assert "/reviews" in source
        assert "pulls/" in source


class TestFetchingEndToEnd:
    """Drive _fetch_pr_review_bodies with a faked gh, both endpoints."""

    @staticmethod
    def _task():
        from unittest.mock import MagicMock

        task = MagicMock()
        task.id = "t1"
        task.pr_url = "https://github.com/o/r/pull/7"
        task.mr_url = ""
        return task

    @staticmethod
    def _gh(reviews_json: str, comments_json: str, comments_rc: int = 0):
        from unittest.mock import MagicMock

        def _run(cmd, **kwargs):
            out = MagicMock()
            if "/comments" in cmd[2]:
                out.returncode = comments_rc
                out.stdout = comments_json
                out.stderr = "" if comments_rc == 0 else "boom"
            else:
                out.returncode = 0
                out.stdout = reviews_json
                out.stderr = ""
            return out

        return _run

    async def test_an_approvers_findings_survive_a_block(self):
        from unittest.mock import patch

        run = self._gh(
            reviews_json='[{"state":"CHANGES_REQUESTED","body":"**[BLOCKER]** fix the lock"},'
            '{"state":"APPROVED","body":"LGTM, but **[nit]** rename the flag"}]',
            comments_json="[]",
        )
        with patch("subprocess.run", new=run):
            out = await _fetch_pr_review_bodies(None, self._task())

        assert "fix the lock" in out
        assert "rename the flag" in out, "the approver's finding must not be dropped"
        assert out.index("fix the lock") < out.index("rename the flag"), "blocking bodies lead"

    async def test_inline_comments_reach_the_checklist_with_their_location(self):
        from unittest.mock import patch

        run = self._gh(
            reviews_json='[{"state":"CHANGES_REQUESTED","body":"see inline"}]',
            comments_json='[{"path":"app/x.py","line":42,"body":"**[WARNING]** off-by-one"}]',
        )
        with patch("subprocess.run", new=run):
            out = await _fetch_pr_review_bodies(None, self._task())

        assert "app/x.py:42" in out
        assert "off-by-one" in out
        assert "1. [ ]" in out, "an inline finding alone must still produce a numbered checklist"

    async def test_a_failed_comments_fetch_still_returns_the_reviews(self):
        from unittest.mock import patch

        run = self._gh(
            reviews_json='[{"state":"CHANGES_REQUESTED","body":"**[BLOCKER]** fix the lock"}]',
            comments_json="",
            comments_rc=1,
        )
        with patch("subprocess.run", new=run):
            out = await _fetch_pr_review_bodies(None, self._task())

        assert "fix the lock" in out
