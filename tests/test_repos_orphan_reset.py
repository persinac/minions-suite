"""A dirty tree left by a DEAD job must not wedge the repo forever.

Checkouts are keyed by repo_path, so every job on a repo shares one working
tree. ensure_checkout refused to reset a dirty tree at all, on the reasoning
that the uncommitted files might belong to a job still working. That is correct
while something is running and wrong the moment nothing is — and nothing ever
cleared it afterwards.

The failure is silent and compounding. A job dies mid-edit; the files stay. The
next job on that repo starts on top of them, so its branch and its PR carry
edits its ticket never asked for, and auto_merge would land them. Every later
job inherits the same dirt. The repo never recovers on its own, and on the
previous Longhorn PVC it did not even recover across pod restarts.

ensure_checkout cannot resolve this itself: it sees uncommitted files and has no
idea whose they are. Only the engine knows whether an agent is running, so the
decision moved there (job_engine._run_in_process) and this module now takes an
explicit reset_dirty.

Runs against a real local bare repo over file://, so the git behaviour under
test is genuine git behaviour.
"""

import subprocess
from pathlib import Path

import pytest

from minions import repos


@pytest.fixture(autouse=True)
def isolate_git_config(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(repos, "_git_configured", False)
    yield


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def origin(tmp_path) -> str:
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


async def _dirty_checkout_on_a_stale_branch(origin: str, dest: Path) -> None:
    """What a job that died mid-edit leaves behind."""
    await repos.ensure_checkout(origin, str(dest), "main")
    _git("checkout", "-b", "feat/job-dead/whatever", cwd=dest)
    (dest / "README.md").write_text("half-finished edit\n")  # tracked, modified
    (dest / "scratch.py").write_text("stray new file\n")  # untracked


class TestLiveWorkIsProtected:
    async def test_a_dirty_tree_is_left_alone_by_default(self, origin, tmp_path):
        """The default must stay non-destructive: an agent may be mid-work."""
        dest = tmp_path / "repos" / "svc"
        await _dirty_checkout_on_a_stale_branch(origin, dest)

        assert await repos.ensure_checkout(origin, str(dest), "main") is True

        assert (dest / "README.md").read_text() == "half-finished edit\n"
        assert (dest / "scratch.py").exists()
        assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=dest) == "feat/job-dead/whatever"

    async def test_reset_dirty_false_is_explicitly_the_same(self, origin, tmp_path):
        dest = tmp_path / "repos" / "svc"
        await _dirty_checkout_on_a_stale_branch(origin, dest)

        await repos.ensure_checkout(origin, str(dest), "main", reset_dirty=False)

        assert (dest / "scratch.py").exists()


class TestOrphanedDirtIsCleared:
    async def test_tracked_modifications_are_discarded(self, origin, tmp_path):
        dest = tmp_path / "repos" / "svc"
        await _dirty_checkout_on_a_stale_branch(origin, dest)

        assert await repos.ensure_checkout(origin, str(dest), "main", reset_dirty=True) is True

        assert (dest / "README.md").read_text() == "hello\n"

    async def test_untracked_files_are_removed(self, origin, tmp_path):
        """reset --hard does not touch untracked files. Without the clean step
        a dead engineer's new source files survive into the next job's diff."""
        dest = tmp_path / "repos" / "svc"
        await _dirty_checkout_on_a_stale_branch(origin, dest)

        await repos.ensure_checkout(origin, str(dest), "main", reset_dirty=True)

        assert not (dest / "scratch.py").exists()

    async def test_it_returns_to_the_default_branch(self, origin, tmp_path):
        dest = tmp_path / "repos" / "svc"
        await _dirty_checkout_on_a_stale_branch(origin, dest)

        await repos.ensure_checkout(origin, str(dest), "main", reset_dirty=True)

        assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=dest) == "main"

    async def test_the_tree_is_fully_clean_afterwards(self, origin, tmp_path):
        """The whole point: the next job starts from a tree indistinguishable
        from a fresh clone."""
        dest = tmp_path / "repos" / "svc"
        await _dirty_checkout_on_a_stale_branch(origin, dest)

        await repos.ensure_checkout(origin, str(dest), "main", reset_dirty=True)

        assert _git("status", "--porcelain", cwd=dest) == ""

    async def test_ignored_build_artefacts_survive(self, origin, tmp_path):
        """`clean -fd`, deliberately not `-fdx`. Blowing away .venv/ and
        node_modules/ on every job is a real cost and buys no correctness."""
        dest = tmp_path / "repos" / "svc"
        await _dirty_checkout_on_a_stale_branch(origin, dest)
        (dest / ".gitignore").write_text(".venv/\n")
        _git("add", ".gitignore", cwd=dest)
        _git("-c", "user.email=t@t.test", "-c", "user.name=t", "commit", "-m", "ignore", cwd=dest)
        (dest / ".venv").mkdir()
        (dest / ".venv" / "pyvenv.cfg").write_text("expensive to rebuild\n")

        await repos.ensure_checkout(origin, str(dest), "main", reset_dirty=True)

        assert (dest / ".venv" / "pyvenv.cfg").exists()


class TestEngineDecidesOwnership:
    """The engine is the only layer that knows whether an agent is running."""

    def test_it_excludes_its_own_agent_from_the_running_check(self):
        """The launching agent is already persisted as 'starting' by this
        point, so an unfiltered get_running_agents() is never empty and
        reset_dirty would never once be True."""
        import inspect

        from minions.engine.job_engine import JobEngine

        source = inspect.getsource(JobEngine._run_in_process)

        assert "get_running_agents()" in source
        assert "a.id != agent.id" in source
        assert "reset_dirty=orphaned" in source
