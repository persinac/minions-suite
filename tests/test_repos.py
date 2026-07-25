"""ensure_checkout — the clone step the in-process agent path was missing.

Runs against a real local bare repo over a file:// URL, so the git behaviour
under test is genuine git behaviour and no network is touched.

configure_git writes to --global, so every test redirects GIT_CONFIG_GLOBAL at a
tmp file. Without that these tests would rewrite the developer's own ~/.gitconfig.
"""

import subprocess
from pathlib import Path

import pytest

from minions import repos


@pytest.fixture(autouse=True)
def isolate_git_config(tmp_path, monkeypatch):
    """Keep --global writes away from the real ~/.gitconfig."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(repos, "_git_configured", False)
    yield


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def origin(tmp_path) -> str:
    """A real bare repo with one commit on `main`, usable as a file:// remote."""
    work = tmp_path / "work"
    work.mkdir()
    _git("init", "-b", "main", cwd=work)
    _git("config", "user.email", "t@t.test", cwd=work)
    _git("config", "user.name", "t", cwd=work)
    (work / "README.md").write_text("hello\n")
    _git("add", "-A", cwd=work)
    _git("commit", "-m", "initial", cwd=work)

    bare = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "--bare", str(work), str(bare)], check=True, capture_output=True)
    return str(bare)


class TestEnsureCheckout:
    async def test_clones_when_missing(self, origin, tmp_path):
        dest = tmp_path / "repos" / "svc"

        assert await repos.ensure_checkout(origin, str(dest), "main") is True

        assert (dest / ".git").is_dir()
        assert (dest / "README.md").read_text() == "hello\n"

    async def test_creates_missing_parent_directories(self, origin, tmp_path):
        """repo_path is /repos/<name>; nothing guarantees intermediate dirs exist."""
        dest = tmp_path / "a" / "b" / "c" / "svc"

        assert await repos.ensure_checkout(origin, str(dest), "main") is True
        assert (dest / "README.md").exists()

    async def test_does_not_reset_an_existing_checkout(self, origin, tmp_path):
        """The PVC is shared. Resetting would delete a running job's work.

        This is the whole reason the second pass fetches instead of resetting,
        so it is asserted directly rather than inferred from the absence of a
        reset call.
        """
        dest = tmp_path / "svc"
        await repos.ensure_checkout(origin, str(dest), "main")

        # Simulate a job mid-flight: a feature branch with uncommitted edits.
        _git("checkout", "-b", "feature/wip", cwd=dest)
        (dest / "in_progress.py").write_text("half-written\n")
        (dest / "README.md").write_text("locally modified\n")

        assert await repos.ensure_checkout(origin, str(dest), "main") is True

        assert (dest / "in_progress.py").read_text() == "half-written\n"
        assert (dest / "README.md").read_text() == "locally modified\n"
        assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=dest) == "feature/wip"

    async def test_second_pass_fetches_new_upstream_commits(self, origin, tmp_path):
        """Not resetting must not mean not fetching — agents branch off origin."""
        dest = tmp_path / "svc"
        await repos.ensure_checkout(origin, str(dest), "main")

        # Land a new commit on the remote.
        upstream = tmp_path / "upstream"
        subprocess.run(["git", "clone", origin, str(upstream)], check=True, capture_output=True)
        _git("config", "user.email", "t@t.test", cwd=upstream)
        _git("config", "user.name", "t", cwd=upstream)
        (upstream / "NEW.md").write_text("new\n")
        _git("add", "-A", cwd=upstream)
        _git("commit", "-m", "second", cwd=upstream)
        _git("push", "origin", "main", cwd=upstream)

        assert await repos.ensure_checkout(origin, str(dest), "main") is True

        # Fetched into the remote-tracking ref, even though the worktree is untouched.
        assert "NEW.md" in _git("show", "--name-only", "origin/main", cwd=dest)

    async def test_replaces_a_non_repo_directory(self, origin, tmp_path):
        """A clone killed partway leaves junk that would make git refuse to clone."""
        dest = tmp_path / "svc"
        dest.mkdir(parents=True)
        (dest / "leftover.txt").write_text("from a failed clone\n")

        assert await repos.ensure_checkout(origin, str(dest), "main") is True

        assert (dest / ".git").is_dir()
        assert not (dest / "leftover.txt").exists()

    async def test_returns_false_without_a_clone_url(self, tmp_path):
        assert await repos.ensure_checkout("", str(tmp_path / "svc"), "main") is False

    async def test_returns_false_when_the_clone_fails(self, tmp_path):
        dest = tmp_path / "svc"

        assert await repos.ensure_checkout(str(tmp_path / "does-not-exist.git"), str(dest), "main") is False

    async def test_honours_a_non_default_branch(self, origin, tmp_path):
        """Five of the registered repos are on `master`, not `main`."""
        upstream = tmp_path / "upstream"
        subprocess.run(["git", "clone", origin, str(upstream)], check=True, capture_output=True)
        _git("config", "user.email", "t@t.test", cwd=upstream)
        _git("config", "user.name", "t", cwd=upstream)
        _git("checkout", "-b", "master", cwd=upstream)
        (upstream / "ON_MASTER.md").write_text("x\n")
        _git("add", "-A", cwd=upstream)
        _git("commit", "-m", "master only", cwd=upstream)
        _git("push", "origin", "master", cwd=upstream)

        dest = tmp_path / "svc"
        assert await repos.ensure_checkout(origin, str(dest), "master") is True

        assert (dest / "ON_MASTER.md").exists()
        assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=dest) == "master"


class TestConfigureGit:
    async def test_sets_identity_so_commits_do_not_abort(self, tmp_path):
        await repos.configure_git()

        cfg = (tmp_path / "gitconfig").read_text()
        assert repos._BOT_EMAIL in cfg
        assert repos._BOT_NAME in cfg

    async def test_credential_helper_defers_to_the_environment(self, tmp_path):
        """The helper must read GH_TOKEN at call time, never store a value.

        A GitHub App token expires after an hour, so a persisted one is both a
        leak and guaranteed to go stale.
        """
        await repos.configure_git()

        cfg = (tmp_path / "gitconfig").read_text()
        assert "GH_TOKEN" in cfg
        assert "x-access-token" in cfg

    async def test_a_real_token_never_reaches_the_config_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GH_TOKEN", "ghs_thisMustNeverBeWrittenToDisk")

        await repos.configure_git()

        assert "ghs_thisMustNeverBeWrittenToDisk" not in (tmp_path / "gitconfig").read_text()

    async def test_is_idempotent(self, tmp_path):
        await repos.configure_git()
        await repos.configure_git()

        assert repos._git_configured is True
