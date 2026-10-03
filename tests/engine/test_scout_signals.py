"""The scout's deterministic signals, measured against a real git repository.

These run real `git` on a throwaway repo rather than faking its output: the
signals ARE git output, and a fake would only test the parser against the
author's idea of what git prints.
"""

import subprocess
from types import SimpleNamespace

import pytest

from minions.engine.scout_signals import churn_hot_spots, collect_signals, is_noise, render_signals, todo_density, worth_a_model_call


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _commit(repo, files: dict[str, str], message: str):
    for path, body in files.items():
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    big = "line\n" * 200
    small = "line\n" * 10
    _commit(root, {"app/big.py": big, "app/small.py": small, "uv.lock": "x\n" * 5000}, "one")
    for i in range(4):
        _commit(root, {"app/big.py": big + f"# {i}\n", "uv.lock": "y\n" * (5000 + i)}, f"churn {i}")
    for i in range(6):
        _commit(root, {"app/small.py": small + f"# {i}\n"}, f"small {i}")
    _commit(root, {"app/todo.py": "# TODO one\n# FIXME two\nx = 1  # HACK three\n", "app/clean.py": "x = 1\n"}, "todos")
    return root


@pytest.mark.asyncio
class TestHotSpots:
    async def test_ranks_by_commits_times_size(self, repo):
        """big.py: 5 commits x ~201 lines beats small.py: 7 commits x ~11 lines."""
        spots = await churn_hot_spots(str(repo))
        assert spots[0]["path"] == "app/big.py"
        assert spots[0]["commits"] == 5
        assert {s["path"] for s in spots} >= {"app/big.py", "app/small.py"}

    async def test_lockfiles_are_noise_however_much_they_churn(self, repo):
        spots = await churn_hot_spots(str(repo))
        assert "uv.lock" not in {s["path"] for s in spots}

    async def test_a_deleted_file_is_not_a_hot_spot(self, repo):
        _git(repo, "rm", "-q", "app/big.py")
        _git(repo, "commit", "-q", "-m", "drop big")
        spots = await churn_hot_spots(str(repo))
        assert "app/big.py" not in {s["path"] for s in spots}


@pytest.mark.asyncio
class TestTodos:
    async def test_counts_marks_per_file(self, repo):
        todos = await todo_density(str(repo))
        assert todos == [{"path": "app/todo.py", "todos": 3}]

    async def test_a_repo_with_no_marks_is_an_empty_answer_not_an_error(self, tmp_path):
        root = tmp_path / "clean"
        root.mkdir()
        _git(root, "init", "-q")
        _git(root, "config", "user.email", "t@example.com")
        _git(root, "config", "user.name", "t")
        _commit(root, {"a.py": "x = 1\n"}, "c")
        assert await todo_density(str(root)) == []


@pytest.mark.asyncio
class TestCollect:
    async def test_a_repo_with_no_test_command_is_flagged(self, repo):
        signals = await collect_signals(str(repo), "svc", SimpleNamespace(test_command="", lint_command="", language="python"))
        assert signals["missing_test_oracle"] is True
        assert worth_a_model_call(signals)

    async def test_a_tested_repo_is_not_flagged(self, repo):
        signals = await collect_signals(str(repo), "svc", SimpleNamespace(test_command="uv run pytest", lint_command="", language="python"))
        assert signals["missing_test_oracle"] is False

    async def test_deferred_signals_are_named_not_silently_absent(self, repo):
        signals = await collect_signals(str(repo), "svc", SimpleNamespace(test_command="x", lint_command="y", language=""))
        assert signals["deferred"], "an unmeasured signal must say so, or an absence reads as a clean result"


def test_nothing_to_point_at_means_no_model_call():
    assert not worth_a_model_call({"missing_test_oracle": False, "hot_spots": [], "todos": []})


def test_the_rendered_signals_tell_the_scout_the_order_rule():
    text = render_signals(
        {"repo": "svc", "churn_days": 90, "hot_spots": [], "todos": [], "missing_test_oracle": True, "deferred": []},
        max_findings=3,
    )
    assert "missing_test_oracle" in text
    assert "at most **3**" in text


@pytest.mark.parametrize("path", ["package-lock.json", "web/node_modules/x/index.js", "dist/app.min.js", "a/b/uv.lock"])
def test_noise_paths(path):
    assert is_noise(path)


def test_source_is_not_noise():
    assert not is_noise("src/app/routes.py")
