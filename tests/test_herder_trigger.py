"""The trigger's decision logic: when to spawn, and — mostly — when not to.

Every branch here is a spend decision. Spawning twice for one task wastes a
subscription session and leaves a second agent racing the first; not spawning at
all means the engine's 900s fallback bills the metered engineer, which is the
cost this whole component exists to remove.

The spawn itself and the MCP call are I/O and are covered by running it; what is
tested here is the arithmetic that decides.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "herder_trigger.py"


@pytest.fixture(scope="module")
def trig():
    """Load the script as a module. It is a script, not a package member."""
    spec = importlib.util.spec_from_file_location("herder_trigger", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["herder_trigger"] = module
    spec.loader.exec_module(module)
    return module


class TestPruning:
    """State says "already spawned for this task". Getting it wrong costs money
    in one direction and duplicates work in the other."""

    def test_a_task_still_waiting_stays_tracked(self, trig):
        """The gap between spawning and the session's claim is the whole reason
        this state exists: peek still lists the task during it."""
        state = {"t1": 1000.0}

        assert trig.prune(state, {"t1"}, now=1010.0) == {"t1": 1000.0}

    def test_a_claimed_task_is_dropped(self, trig):
        """Leaving the queue means somebody claimed it — ours or otherwise."""
        state = {"t1": 1000.0}

        assert trig.prune(state, set(), now=1010.0) == {}

    def test_a_stale_entry_expires(self, trig):
        """A pane that died before claiming must not block that task forever."""
        state = {"t1": 1000.0}
        later = 1000.0 + trig.SPAWN_TTL_SECONDS + 1

        assert trig.prune(state, {"t1"}, now=later) == {}

    def test_entries_are_independent(self, trig):
        state = {"fresh": 1000.0, "stale": 0.0}

        pruned = trig.prune(state, {"fresh", "stale"}, now=1010.0)

        assert "fresh" in pruned
        assert "stale" not in pruned, "TTL must be per entry, not per file"


class TestWorkingDirectory:
    def test_a_local_checkout_is_preferred(self, trig, tmp_path, monkeypatch):
        repo = tmp_path / "healthcheck"
        (repo / ".git").mkdir(parents=True)
        monkeypatch.setenv("REPO_DIR", str(tmp_path))

        assert trig.working_dir({"service": "healthcheck"}) == str(repo)

    def test_the_personal_subdir_is_searched_too(self, trig, tmp_path, monkeypatch):
        repo = tmp_path / "personal" / "minions-suite"
        (repo / ".git").mkdir(parents=True)
        monkeypatch.setenv("REPO_DIR", str(tmp_path))

        assert trig.working_dir({"service": "minions-suite"}) == str(repo)

    def test_an_unknown_service_falls_back_to_the_repo_root(self, trig, tmp_path, monkeypatch):
        """Not an error: the herd skill clones from clone_url itself, so the cwd
        only has to be somewhere sensible to start."""
        monkeypatch.setenv("REPO_DIR", str(tmp_path))

        assert trig.working_dir({"service": "never-heard-of-it"}) == str(tmp_path)

    def test_a_directory_without_git_is_not_treated_as_a_checkout(self, trig, tmp_path, monkeypatch):
        (tmp_path / "healthcheck").mkdir()
        monkeypatch.setenv("REPO_DIR", str(tmp_path))

        assert trig.working_dir({"service": "healthcheck"}) == str(tmp_path)


class TestSpawnCommand:
    def test_dry_mode_runs_nothing(self, trig, monkeypatch):
        """The point of dry mode. If this ever shells out, the mode is a lie."""
        called = []
        monkeypatch.setattr(trig.subprocess, "run", lambda *a, **k: called.append(a) or None)

        assert trig.spawn({"task_id": "abcdef1234", "service": "healthcheck", "title": "t"}, dry=True) is True
        assert called == []

    def test_the_seed_prompt_names_the_task(self, trig):
        """The pane must know which item it was started for; a bare '/herd' would
        claim whatever happens to be first, which may not be what we counted."""
        seed = trig.SEED_PROMPT.format(task_id="abcdef1234", service="healthcheck")

        assert "abcdef1234" in seed
        assert "healthcheck" in seed
        assert "/herd" in seed

    def test_the_spawn_goes_through_open_claude(self, trig, monkeypatch):
        """Bare `claude` would skip registry + Slack-bus registration, so the
        herder would be invisible to the fleet while it worked."""
        captured = {}

        class Done:
            returncode = 0
            stderr = ""

        monkeypatch.setattr(trig.subprocess, "run", lambda argv, **k: captured.update(argv=argv) or Done())
        trig.spawn({"task_id": "abcdef1234", "service": "healthcheck", "title": "t"}, dry=False)

        argv = captured["argv"]
        assert argv[1] == "spawn"
        assert "open-claude.sh" in " ".join(argv)
        assert "SEED_PROMPT=" in " ".join(argv)
        assert "--workspace" in argv

    def test_a_failed_spawn_is_reported_not_swallowed(self, trig, monkeypatch):
        """A spawn that silently 'succeeded' would be recorded in state and block
        that task until the TTL, with no herder actually running."""

        class Failed:
            returncode = 1
            stderr = "herdr: agent_name_taken"

        monkeypatch.setattr(trig.subprocess, "run", lambda *a, **k: Failed())

        assert trig.spawn({"task_id": "abcdef1234", "service": "x", "title": "t"}, dry=False) is False


class TestSafetyDefaults:
    def test_mode_is_off_when_the_host_has_not_opted_in(self, monkeypatch):
        """A git pull must not turn a machine into an agent spawner.

        Reloaded with the variable removed, because the module reads it at import
        and the fixture's copy may have been imported under whatever this shell
        happens to export.
        """
        import importlib.util

        monkeypatch.delenv("MINIONS_HERDER_MODE", raising=False)
        spec = importlib.util.spec_from_file_location("herder_trigger_clean", SCRIPT)
        clean = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(clean)

        assert clean.MODE == "off"

    def test_live_must_be_spelled_exactly(self, monkeypatch):
        """`spawn(dry=MODE != "live")` — anything unrecognised must stay dry."""
        import importlib.util

        monkeypatch.setenv("MINIONS_HERDER_MODE", "LIVE-ish")
        spec = importlib.util.spec_from_file_location("herder_trigger_typo", SCRIPT)
        typo = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(typo)

        assert typo.MODE != "live", "a typo'd mode must not spawn"

    def test_there_is_a_concurrency_cap(self, trig):
        assert trig.MAX_HERDERS >= 1

    def test_the_ttl_outlasts_a_slow_startup(self, trig):
        """Shorter than pane-startup and the trigger double-spawns every item."""
        assert trig.SPAWN_TTL_SECONDS >= 120
