"""Tests for git provider abstraction."""

import pytest

from minions.providers.git import (
    GitHubProvider,
    GitLabProvider,
    GitProviderProtocol,
    InlineComment,
    PRInfo,
    create_provider,
)


class TestCreateProvider:
    def test_gitlab_provider(self):
        provider = create_provider("gitlab", token="test-token", gitlab_url="https://gitlab.example.com")
        assert isinstance(provider, GitLabProvider)

    def test_github_provider(self):
        provider = create_provider("github", token="test-token")
        assert isinstance(provider, GitHubProvider)

    def test_unsupported_provider_raises(self):
        with pytest.raises(ValueError, match="Unsupported"):
            create_provider("bitbucket")

    def test_gitlab_requires_url(self):
        with pytest.raises(ValueError, match="gitlab_url"):
            create_provider("gitlab", token="tok")

    def test_gitlab_requires_token(self):
        with pytest.raises(ValueError, match="token"):
            create_provider("gitlab", gitlab_url="https://gl.example.com")


class TestInlineComment:
    def test_construction(self):
        c = InlineComment(file_path="foo.py", line=10, body="Bug here")
        assert c.file_path == "foo.py"
        assert c.line == 10
        assert c.body == "Bug here"
        assert c.side == "RIGHT"

    def test_custom_side(self):
        c = InlineComment(file_path="foo.py", line=5, body="Old code", side="LEFT")
        assert c.side == "LEFT"


class TestPRInfo:
    def test_construction(self):
        pr = PRInfo(
            id="42", url="https://example.com/mr/42", title="Fix bug",
            description="Details", author="dev", branch="fix/bug",
            target_branch="main", state="open",
        )
        assert pr.id == "42"
        assert pr.state == "open"


class TestProtocol:
    def test_gitlab_implements_protocol(self):
        provider = create_provider("gitlab", token="tok", gitlab_url="https://gl.example.com")
        assert isinstance(provider, GitProviderProtocol)

    def test_github_implements_protocol(self):
        provider = create_provider("github", token="tok")
        assert isinstance(provider, GitProviderProtocol)
