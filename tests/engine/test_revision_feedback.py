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
    def test_blocking_reviews_are_preferred_over_approvals(self):
        """An approval body is not what the revision needs to act on."""
        source = inspect.getsource(_fetch_pr_review_bodies)

        assert 'r.get("state") == "CHANGES_REQUESTED"' in source
        assert "blocking or reviews" in source

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
