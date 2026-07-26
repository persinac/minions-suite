"""Real inline review comments on GitHub, with a fallback that loses nothing.

post_inline_comment always degraded to a top-level PR comment, on the belief that
gh could not post inline. `gh pr comment` cannot; `gh api` can — it needs the head
SHA and a JSON body rather than form fields.

This matters for the reviewer swarm: every persona mandates exactly one
`<new-path>:<line>` anchor per finding, specifically so it lands on the offending
line. Unanchored, five specialists' findings arrive as one undifferentiated blob.
"""

import json

import pytest

from minions.providers.git import GitHubProvider, InlineComment


def _comment(path="app/crud/play_transaction.py", line=142):
    return InlineComment(file_path=path, line=line, body="**[CRITICAL][DB]** missing index\n\nWhy: seq scan\nFix: add one")


class _Gh:
    """Records gh invocations; can be told to fail the inline POST."""

    def __init__(self, fail_inline=False, fail_sha=False):
        self.calls: list[tuple[list[str], str | None]] = []
        self.fail_inline = fail_inline
        self.fail_sha = fail_sha

    def __call__(self, args, timeout=30, stdin=None):
        self.calls.append((args, stdin))
        if "--jq" in args and ".head.sha" in args:
            if self.fail_sha:
                raise RuntimeError("HTTP 404")
            return "deadbeefcafe1234"
        if "pulls" in " ".join(args) and "--method" in args:
            if self.fail_inline:
                raise RuntimeError("HTTP 422: line must be part of the diff")
            return "{}"
        return ""

    @property
    def posted_inline(self):
        return any("--method" in a and "comments" in " ".join(a) for a, _ in self.calls)

    @property
    def posted_top_level(self):
        return any(a[:2] == ["pr", "comment"] for a, _ in self.calls)


@pytest.fixture
def provider(monkeypatch):
    p = GitHubProvider(token="t")
    return p


class TestInlineHappyPath:
    async def test_posts_a_real_inline_comment(self, provider, monkeypatch):
        gh = _Gh()
        monkeypatch.setattr(provider, "_run_gh", gh)

        result = await provider.post_inline_comment("org/repo", "23", _comment())

        assert result["inline"] is True
        assert gh.posted_inline
        assert not gh.posted_top_level, "must not also post a duplicate top-level comment"

    async def test_payload_carries_the_anchor_and_head_sha(self, provider, monkeypatch):
        gh = _Gh()
        monkeypatch.setattr(provider, "_run_gh", gh)

        await provider.post_inline_comment("org/repo", "23", _comment(line=142))

        _, stdin = next((c for c in gh.calls if "--method" in c[0]), (None, None))
        payload = json.loads(stdin)

        assert payload["path"] == "app/crud/play_transaction.py"
        assert payload["line"] == 142
        assert payload["side"] == "RIGHT"
        assert payload["commit_id"] == "deadbeefcafe1234"

    async def test_body_goes_through_stdin_not_argv(self, provider, monkeypatch):
        """Bodies contain newlines, quotes and backticks — argv is a quoting trap."""
        gh = _Gh()
        monkeypatch.setattr(provider, "_run_gh", gh)

        await provider.post_inline_comment("org/repo", "23", _comment())

        args, stdin = next(c for c in gh.calls if "--method" in c[0])
        assert stdin is not None, "payload must be piped, not passed as arguments"
        assert "--input" in args
        assert not any("missing index" in a for a in args), "body leaked into argv"


class TestFallback:
    async def test_unanchorable_line_degrades_instead_of_raising(self, provider, monkeypatch):
        """GitHub 422s a line that is not in the diff. Do not lose the finding."""
        gh = _Gh(fail_inline=True)
        monkeypatch.setattr(provider, "_run_gh", gh)

        result = await provider.post_inline_comment("org/repo", "23", _comment())

        assert result["posted"] is True
        assert result["inline"] is False
        assert gh.posted_top_level

    async def test_fallback_keeps_the_location_in_the_text(self, provider, monkeypatch):
        gh = _Gh(fail_inline=True)
        monkeypatch.setattr(provider, "_run_gh", gh)

        await provider.post_inline_comment("org/repo", "23", _comment(line=142))

        args, _ = next(c for c in gh.calls if c[0][:2] == ["pr", "comment"])
        body = args[args.index("--body") + 1]

        assert "app/crud/play_transaction.py:142" in body
        assert "missing index" in body
        assert "not anchored inline" in body, "the reader should know the anchor was lost"

    async def test_missing_head_sha_still_posts(self, provider, monkeypatch):
        gh = _Gh(fail_sha=True)
        monkeypatch.setattr(provider, "_run_gh", gh)

        result = await provider.post_inline_comment("org/repo", "23", _comment())

        assert result["posted"] is True
        assert result["inline"] is False
        assert gh.posted_top_level

    async def test_a_finding_is_never_silently_dropped(self, provider, monkeypatch):
        """Whatever fails, the finding reaches the PR somehow."""
        for gh in (_Gh(), _Gh(fail_inline=True), _Gh(fail_sha=True)):
            monkeypatch.setattr(provider, "_run_gh", gh)
            result = await provider.post_inline_comment("org/repo", "23", _comment())

            assert result["posted"] is True
            assert gh.posted_inline or gh.posted_top_level
