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


class TestReviewerConfigIsActuallyWired:
    """A dataclass field with no from_env mapping is dead config.

    Shipped exactly that in 0.2.1: the three reviewer fields existed on Config
    but nothing read the environment into them, so build_reviewer_token_provider
    always saw "" and returned None. The second App could have been created,
    installed and its secrets set, and reviews would still have silently fallen
    back to comments with nothing to indicate why.
    """

    def test_reviewer_app_env_vars_reach_config(self, monkeypatch):
        from minions.config import Config

        monkeypatch.setenv("GITHUB_APP_REVIEWER_ID", "5555555")
        monkeypatch.setenv("GITHUB_APP_REVIEWER_PRIVATE_KEY", "-----BEGIN RSA PRIVATE KEY-----\nk\n-----END RSA PRIVATE KEY-----")
        monkeypatch.setenv("GITHUB_APP_REVIEWER_INSTALLATION_ID", "222")

        config = Config.from_env()

        assert config.github_reviewer_app_id == "5555555"
        assert "BEGIN RSA PRIVATE KEY" in config.github_reviewer_app_private_key
        assert config.github_reviewer_app_installation_id == "222"

    def test_the_env_wiring_produces_a_working_provider(self, monkeypatch):
        """End to end: env -> Config -> provider, the path that was broken."""
        from minions.config import Config
        from minions.providers.github_app import build_reviewer_token_provider

        monkeypatch.setenv("GITHUB_APP_ID", "4393069")
        monkeypatch.setenv("GITHUB_APP_REVIEWER_ID", "5555555")
        monkeypatch.setenv("GITHUB_APP_REVIEWER_PRIVATE_KEY", "-----BEGIN RSA PRIVATE KEY-----\nk\n-----END RSA PRIVATE KEY-----")
        monkeypatch.setenv("GITHUB_APP_REVIEWER_INSTALLATION_ID", "222")

        provider = build_reviewer_token_provider(Config.from_env())

        assert provider is not None
        assert provider.app_id == "5555555"

    def test_engineer_app_env_wiring_still_works(self, monkeypatch):
        from minions.config import Config

        monkeypatch.setenv("GITHUB_APP_ID", "4393069")
        monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "148993220")

        config = Config.from_env()

        assert config.github_app_id == "4393069"
        assert config.github_app_installation_id == "148993220"


class TestMintDoesNotStealTheGitIdentity:
    """GH_TOKEN is the identity that clones, commits and pushes.

    _mint() is shared by both providers and wrote os.environ["GH_TOKEN"]
    unconditionally, so the reviewer minting a token silently reattributed every
    subsequent git operation to the reviewer App. Confirmed against the live
    credentials before the fix: after a reviewer mint, GH_TOKEN held the
    reviewer's token.

    The earlier no-clobber test passed throughout, because it exercised the
    unconfigured path that returns None without ever minting. These mint for real.
    """

    @staticmethod
    def _provider(monkeypatch, token_value: str, **kwargs):
        from minions.providers.github_app import GitHubAppTokenProvider

        p = GitHubAppTokenProvider(
            app_id="123",
            private_key="-----BEGIN RSA PRIVATE KEY-----\nk\n-----END RSA PRIVATE KEY-----",
            installation_id="456",
            **kwargs,
        )

        class _Resp:
            status_code = 201

            @staticmethod
            def json():
                return {"token": token_value, "expires_at": "2099-01-01T00:00:00Z"}

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, *a, **kw):
                return _Resp()

        monkeypatch.setattr(p, "_build_jwt", lambda: "jwt")
        monkeypatch.setattr("minions.providers.github_app.httpx.AsyncClient", lambda **kw: _Client())
        return p

    async def test_engineer_mint_sets_gh_token(self, monkeypatch):
        """The engineer App must keep exporting — ambient `gh` callers rely on it."""
        import os

        monkeypatch.setenv("GH_TOKEN", "stale")
        p = self._provider(monkeypatch, "ghs_engineer", export_env=True)

        await p.token()

        assert os.environ["GH_TOKEN"] == "ghs_engineer"

    async def test_reviewer_mint_leaves_gh_token_alone(self, monkeypatch):
        """The bug: a reviewer mint used to overwrite the engineer's identity."""
        import os

        monkeypatch.setenv("GH_TOKEN", "ghs_engineer")
        p = self._provider(monkeypatch, "ghs_reviewer", export_env=False)

        token = await p.token()

        assert token == "ghs_reviewer", "the caller still gets the reviewer token"
        assert os.environ["GH_TOKEN"] == "ghs_engineer", "GH_TOKEN was hijacked by the reviewer"

    def test_the_reviewer_factory_disables_export(self):
        """Guards the wiring, not just the capability."""
        from minions.config import Config
        from minions.providers.github_app import build_reviewer_token_provider

        config = Config.from_env()
        config.github_app_id = "4393069"
        config.github_reviewer_app_id = "4394037"
        config.github_reviewer_app_private_key = "-----BEGIN RSA PRIVATE KEY-----\nk\n-----END RSA PRIVATE KEY-----"
        config.github_reviewer_app_installation_id = "222"

        assert build_reviewer_token_provider(config).export_env is False

    def test_the_engineer_factory_keeps_export(self):
        from minions.config import Config
        from minions.providers.github_app import build_token_provider

        config = Config.from_env()
        config.github_app_id = "4393069"
        config.github_app_private_key = "-----BEGIN RSA PRIVATE KEY-----\nk\n-----END RSA PRIVATE KEY-----"
        config.github_app_installation_id = "148993220"

        assert build_token_provider(config).export_env is True
