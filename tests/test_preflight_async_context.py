"""run_preflight must work from BOTH a sync caller and inside a running loop.

`minion --preflight` calls it with no event loop; _run_server (cli.py:270) calls
it from inside asyncio.run(). A check that uses asyncio.run() internally works in
the first and raises "asyncio.run() cannot be called from a running event loop"
in the second — which crashlooped the server on every start while the provider's
own unit tests passed, because they exercised the provider directly and never the
preflight path from an async caller.
"""

from dataclasses import dataclass, field

import pytest

from minions.preflight import FAIL, PASS, _run_async, check_git_provider


@dataclass
class _Cfg:
    """Minimal stand-in for Config — only what check_git_provider reads."""

    git_provider: str = "github"
    github_token: str = ""
    gitlab_token: str = ""
    gitlab_url: str = ""
    github_app_id: str = ""
    github_app_private_key: str = ""
    github_app_installation_id: str = ""
    _extra: dict = field(default_factory=dict)


class TestRunAsyncHelper:
    def test_works_with_no_running_loop(self):
        async def coro():
            return "sync-context"

        assert _run_async(coro) == "sync-context"

    async def test_works_inside_a_running_loop(self):
        """The case that crashed production."""

        async def coro():
            return "async-context"

        assert _run_async(coro) == "async-context"

    async def test_propagates_exceptions(self):
        async def boom():
            raise ValueError("deliberate")

        with pytest.raises(ValueError, match="deliberate"):
            _run_async(boom)


class TestCheckGitProviderFromAsyncContext:
    """check_git_provider is sync but is invoked from inside asyncio.run()."""

    async def test_app_path_does_not_raise_from_a_running_loop(self, monkeypatch):
        """Regression: this raised RuntimeError and killed the server on boot."""

        class _StubProvider:
            async def token(self):
                return "ghs_stub"

        monkeypatch.setattr("minions.providers.github_app.build_token_provider", lambda _c: _StubProvider())

        cfg = _Cfg(github_app_id="4393069", github_app_private_key="pem", github_app_installation_id="12345")

        # Assert on the value, not just absence of a raise: a check that returned
        # FAIL("cannot be called from a running event loop") would also "not raise".
        result = check_git_provider(cfg)
        assert result.status == PASS
        assert "4393069" in result.detail

    async def test_app_failure_reports_fail_rather_than_exploding(self, monkeypatch):
        from minions.providers.github_app import GitHubAppError

        class _BadProvider:
            async def token(self):
                raise GitHubAppError("installation 999 not found (404)")

        monkeypatch.setattr("minions.providers.github_app.build_token_provider", lambda _c: _BadProvider())

        cfg = _Cfg(github_app_id="4393069", github_app_private_key="pem", github_app_installation_id="999")

        result = check_git_provider(cfg)
        assert result.status == FAIL
        assert "404" in result.detail

    async def test_static_token_path_still_works(self):
        assert check_git_provider(_Cfg(github_token="ghp_x")).status == PASS

    def test_app_path_also_works_with_no_loop(self, monkeypatch):
        """The `minion --preflight` CLI entry point."""

        class _StubProvider:
            async def token(self):
                return "ghs_stub"

        monkeypatch.setattr("minions.providers.github_app.build_token_provider", lambda _c: _StubProvider())
        cfg = _Cfg(github_app_id="4393069", github_app_private_key="pem", github_app_installation_id="12345")

        assert check_git_provider(cfg).status == PASS
