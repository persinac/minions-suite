"""The trigger's decision logic: when to spawn, and — mostly — when not to.

Every branch here is a spend decision. Spawning twice for one task wastes a
subscription session and leaves a second agent racing the first; not spawning at
all means the engine's 900s fallback bills the metered engineer, which is the
cost this whole component exists to remove.

The spawn itself and the MCP call are I/O and are covered by running it; what is
tested here is the arithmetic that decides.
"""

import importlib.util
import os
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
    """The pane must start where /herd exists, which is not the target repo.

    /herd is a PROJECT skill under this repo's .claude/skills/ and is not
    symlinked into ~/.claude/skills/. The first version of working_dir() started
    the pane in the target service's checkout, where /herd does not exist — the
    seed prompt would have told the agent to run a skill it could not see, on the
    very first live spawn.
    """

    def test_it_starts_in_the_repo_that_owns_the_skill(self, trig):
        cwd = Path(trig.working_dir({"service": "healthcheck"}))

        assert (cwd / ".claude" / "skills" / "herd" / "SKILL.md").is_file(), f"/herd is not reachable from {cwd}"

    def test_the_service_does_not_change_it(self, trig):
        """The skill fetches its own code from clone_url, so the target service
        has no bearing on where the pane starts."""
        a = trig.working_dir({"service": "healthcheck"})
        b = trig.working_dir({"service": "management-api"})

        assert a == b

    def test_the_skill_is_not_available_globally(self):
        """Guards the assumption the fix rests on. If herd ever IS symlinked into
        ~/.claude/skills/, starting in the target repo becomes viable again and
        this test should be the thing that says so.
        """
        assert not (Path.home() / ".claude" / "skills" / "herd").exists(), (
            "herd is now global — working_dir() could start in the target repo, and this test should be revisited"
        )


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

    def test_the_command_is_one_argument_not_pre_wrapped(self, trig, monkeypatch):
        """substrate.sh joins everything after <cwd> and wraps it in its own
        `sh -c`. Passing `sh -c <cmd>` double-wraps: the joined string became
        `sh -c SEED_PROMPT="You are the herder. …`, the inner shell word-split on
        the first space, and the pane died instantly while herdr reported
        success. The spawn returned 0 and left nothing behind.
        """
        captured = {}

        class Done:
            returncode = 0
            stderr = ""

        monkeypatch.setattr(trig.subprocess, "run", lambda argv, **k: captured.update(argv=argv) or Done())
        trig.spawn({"task_id": "abcdef1234", "service": "healthcheck", "title": "t"}, dry=False)

        argv = captured["argv"]
        assert "sh" not in argv, "substrate.sh adds its own sh -c; pre-wrapping shatters the quoting"
        assert "-c" not in argv
        # cwd is argv[3]; the command is a single argv[4].
        command = argv[4]
        assert command.startswith("SEED_PROMPT=")
        assert command.endswith("open-claude.sh'") or command.endswith("open-claude.sh")

    def test_it_spawns_unattended_not_manual(self, trig, monkeypatch):
        """A herder that stops to ask is not a herder.

        At the default posture the pane comes up "manual" and halts at the first
        MCP call with "Do you want to proceed?" — nobody is there, so it waits
        until the 900s fallback fires and the metered engineer runs anyway. The
        component's entire purpose, defeated one layer above itself.
        """
        captured = {}

        class Done:
            returncode = 0
            stderr = ""

        monkeypatch.setattr(trig.subprocess, "run", lambda argv, **k: captured.update(argv=argv) or Done())
        trig.spawn({"task_id": "abcdef1234", "service": "healthcheck", "title": "t"}, dry=False)

        command = captured["argv"][4]
        assert "CLAUDE_EXTRA_ARGS=" in command
        assert "--dangerously-skip-permissions" in command

    def test_it_does_not_use_bypass_permissions(self, trig):
        """bypassPermissions trades a per-tool prompt for a startup prompt.

        It opens a one-time "you accept all responsibility" consent dialog on
        every fresh session — the acceptance is persisted nowhere in
        ~/.claude.json — so an unattended herder blocks at startup instead of at
        its first tool call. Observed on a real spawn; `auto` came up running.
        """
        assert "bypassPermissions" not in trig.CLAUDE_EXTRA_ARGS
        assert "auto" not in trig.CLAUDE_EXTRA_ARGS

    def test_the_permission_mode_is_overridable(self, trig):
        """A host that wants to watch the first few runs should be able to."""
        assert os.environ.get("MINIONS_HERDER_CLAUDE_ARGS", "--dangerously-skip-permissions") == trig.CLAUDE_EXTRA_ARGS

    def test_the_seed_survives_shell_quoting(self, trig, monkeypatch):
        """The prompt contains spaces, a slash and parentheses. Unquoted, the
        shell splits it and the agent starts with a fragment or not at all."""
        import shlex

        captured = {}

        class Done:
            returncode = 0
            stderr = ""

        monkeypatch.setattr(trig.subprocess, "run", lambda argv, **k: captured.update(argv=argv) or Done())
        trig.spawn({"task_id": "abcdef1234", "service": "healthcheck", "title": "t"}, dry=False)

        command = captured["argv"][4]
        # Parse it the way `sh -c` would; the assignment must survive intact.
        parsed = shlex.split(command)
        assignment = parsed[0]
        assert assignment.startswith("SEED_PROMPT=")
        assert "/herd" in assignment, "the whole prompt must survive, not just its first word"
        assert "abcdef1234" in assignment

    def test_a_failed_spawn_is_reported_not_swallowed(self, trig, monkeypatch):
        """A spawn that silently 'succeeded' would be recorded in state and block
        that task until the TTL, with no herder actually running."""

        class Failed:
            returncode = 1
            stderr = "herdr: agent_name_taken"

        monkeypatch.setattr(trig.subprocess, "run", lambda *a, **k: Failed())

        assert trig.spawn({"task_id": "abcdef1234", "service": "x", "title": "t"}, dry=False) is False


class TestDryDoesNotConsumeTheBudget:
    """`dry` then `live` must actually spawn.

    The state file means "already spawned, waiting for it to claim". Writing a
    dry run into it made the live run that followed skip the item as work
    somebody else had taken — so the intended workflow (look at what it would
    do, then let it do it) silently did nothing. Caught against real waiting
    work, not in review.
    """

    async def test_a_dry_tick_leaves_the_state_file_empty(self, trig, tmp_path, monkeypatch):
        monkeypatch.setattr(trig, "MODE", "dry")
        monkeypatch.setattr(trig, "STATE_FILE", tmp_path / "spawned.json")
        monkeypatch.setattr(trig, "STATE_DIR", tmp_path)
        monkeypatch.setattr(trig, "tunnel_healthy", lambda: True)

        async def fake_peek():
            return [{"task_id": "abcdef12", "service": "healthcheck", "title": "t", "job_id": "j"}]

        monkeypatch.setattr(trig, "peek", fake_peek)

        await trig.tick()

        assert trig.load_state() == {}, "a dry run must not claim the spawn budget"

    async def test_a_live_tick_records_the_spawn(self, trig, tmp_path, monkeypatch):
        """The other half: a real spawn must be tracked, or the next tick
        double-spawns while the first pane is still starting up."""
        monkeypatch.setattr(trig, "MODE", "live")
        monkeypatch.setattr(trig, "STATE_FILE", tmp_path / "spawned.json")
        monkeypatch.setattr(trig, "STATE_DIR", tmp_path)
        monkeypatch.setattr(trig, "tunnel_healthy", lambda: True)
        monkeypatch.setattr(trig, "spawn", lambda item, dry: True)

        async def fake_peek():
            return [{"task_id": "abcdef12", "service": "healthcheck", "title": "t", "job_id": "j"}]

        monkeypatch.setattr(trig, "peek", fake_peek)

        await trig.tick()

        assert "abcdef12" in trig.load_state()


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
