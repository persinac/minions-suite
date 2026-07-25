"""GitHub refuses a formal review on a PR the same identity authored.

    GraphQL: Review Can not approve your own pull request (addPullRequestReview)

The minion GitHub App both opens the PR and reviews it, so this fires on every
minion-authored PR. It is a platform rule — no token scope makes it work.

Before the fallback, submit_review raised and the reviewer's whole analysis was
thrown away. Observed on job f6451f44: two reviewer agents, $4.87, and
"reviews: NONE POSTED" against the live PR.
"""

import pytest

from minions.providers.git import GitHubProvider

SELF_REVIEW_ERROR = "gh pr review 23 failed: GraphQL: Review Can not approve your own pull request (addPullRequestReview)"


@pytest.fixture
def provider(monkeypatch):
    """A GitHubProvider whose gh calls are recorded instead of executed."""
    p = GitHubProvider(token="fake-token")
    calls: list[list[str]] = []

    def _run(args, timeout=30):
        calls.append(args)
        if "review" in args:
            raise RuntimeError(SELF_REVIEW_ERROR)
        return ""

    monkeypatch.setattr(p, "_run_gh", _run)
    p._calls = calls
    return p


class TestSelfReviewFallback:
    @pytest.mark.parametrize("verdict", ["approve", "request_changes"])
    async def test_falls_back_to_a_comment(self, provider, verdict):
        result = await provider.submit_review("org/repo", "23", verdict, "Looks fine.")

        assert result["posted"] is True
        assert result["as"] == "comment"
        assert result["verdict"] == verdict

    async def test_the_review_body_survives(self, provider):
        """The whole point: the analysis must reach the PR."""
        body = "The fixture uses join_transaction_mode=create_savepoint, which is correct."

        await provider.submit_review("org/repo", "23", "approve", body)

        comment = next(c for c in provider._calls if "comment" in c)
        assert body in comment[comment.index("--body") + 1]

    async def test_the_verdict_is_stated_in_the_comment(self, provider):
        """GitHub will not record the verdict, so the text has to carry it."""
        await provider.submit_review("org/repo", "23", "request_changes", "Needs work.")
        comment = next(c for c in provider._calls if "comment" in c)
        text = comment[comment.index("--body") + 1]

        assert "CHANGES REQUESTED" in text

        provider._calls.clear()
        await provider.submit_review("org/repo", "23", "approve", "Fine.")
        comment = next(c for c in provider._calls if "comment" in c)
        assert "APPROVED" in comment[comment.index("--body") + 1]

    async def test_it_tries_the_real_review_first(self, provider):
        """The fallback must not become the default — a review is still better."""
        await provider.submit_review("org/repo", "23", "approve", "ok")

        assert any("review" in c for c in provider._calls), "never attempted a formal review"
        assert provider._calls[0][1] == "review"

    async def test_unrelated_failures_still_raise(self, monkeypatch):
        """Only the self-review rule is swallowed; a real error must surface."""
        p = GitHubProvider(token="fake-token")

        def _run(args, timeout=30):
            raise RuntimeError("gh pr review 23 failed: HTTP 404: Not Found")

        monkeypatch.setattr(p, "_run_gh", _run)

        with pytest.raises(RuntimeError, match="404"):
            await p.submit_review("org/repo", "23", "approve", "body")

    async def test_a_successful_review_does_not_also_comment(self, monkeypatch):
        """No double-posting when GitHub accepts the review."""
        p = GitHubProvider(token="fake-token")
        calls: list[list[str]] = []

        def _run(args, timeout=30):
            calls.append(args)
            return ""

        monkeypatch.setattr(p, "_run_gh", _run)

        result = await p.submit_review("org/repo", "23", "approve", "body")

        assert result["as"] == "review"
        assert not any("comment" in c for c in calls)


class TestReviewerIdentity:
    """A second App is what makes a real review possible; the fallback is a net."""

    def test_unconfigured_returns_none_so_callers_degrade(self):
        from minions.config import Config
        from minions.providers.github_app import build_reviewer_token_provider

        config = Config.from_env()
        config.github_reviewer_app_id = ""
        config.github_reviewer_app_private_key = ""
        config.github_reviewer_app_installation_id = ""

        assert build_reviewer_token_provider(config) is None

    def test_partial_credentials_return_none_rather_than_half_working(self):
        from minions.config import Config
        from minions.providers.github_app import build_reviewer_token_provider

        config = Config.from_env()
        config.github_reviewer_app_id = "999"
        config.github_reviewer_app_private_key = ""
        config.github_reviewer_app_installation_id = "111"

        assert build_reviewer_token_provider(config) is None

    def test_reusing_the_engineer_app_is_refused(self):
        """Same App means same identity means GitHub rejects the review again."""
        from minions.config import Config
        from minions.providers.github_app import build_reviewer_token_provider

        config = Config.from_env()
        config.github_app_id = "4393069"
        config.github_reviewer_app_id = "4393069"
        config.github_reviewer_app_private_key = "-----BEGIN RSA PRIVATE KEY-----\nx\n-----END RSA PRIVATE KEY-----"
        config.github_reviewer_app_installation_id = "111"

        assert build_reviewer_token_provider(config) is None

    def test_a_distinct_app_builds_a_provider(self):
        from minions.config import Config
        from minions.providers.github_app import build_reviewer_token_provider

        config = Config.from_env()
        config.github_app_id = "4393069"
        config.github_reviewer_app_id = "5555555"
        config.github_reviewer_app_private_key = "-----BEGIN RSA PRIVATE KEY-----\nx\n-----END RSA PRIVATE KEY-----"
        config.github_reviewer_app_installation_id = "222"

        provider = build_reviewer_token_provider(config)

        assert provider is not None
        assert provider.app_id == "5555555"
        assert provider.installation_id == "222"

    async def test_reviewer_token_does_not_clobber_gh_token(self, monkeypatch):
        """GH_TOKEN is the engineer identity — clones and pushes depend on it.

        Overwriting it with the reviewer credential would silently reattribute
        every subsequent git operation.
        """
        import os

        from minions.config import Config
        from minions.providers.github_app import reset_token_provider, reviewer_token

        reset_token_provider()
        monkeypatch.setenv("GH_TOKEN", "engineer-token")

        config = Config.from_env()
        config.github_reviewer_app_id = ""

        assert await reviewer_token(config) is None
        assert os.environ["GH_TOKEN"] == "engineer-token"

        reset_token_provider()
