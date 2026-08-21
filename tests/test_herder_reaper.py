"""Which herder panes get closed, and — more importantly — which do not.

A Claude session does not exit when its work is done; it sits at the prompt. So
every work item used to leak a pane, and a revision round leaked another for the
SAME task. `scripts/herder_trigger.py` now reaps, and the rule lives in
`reap_plan` as a pure function precisely so it can be tested without herdr, an
MCP server, or a real pane.

The dangerous direction is over-reaping: closing a pane mid-work throws away a
subscription-billed run and hands the task back to the metered path, which is
the exact cost this mechanism exists to avoid. So the "must NOT reap" cases
below carry more weight than the "must reap" ones.
"""

import importlib.util
import pathlib

import pytest

_SPEC = importlib.util.spec_from_file_location("herder_trigger", pathlib.Path(__file__).resolve().parents[1] / "scripts" / "herder_trigger.py")
herder_trigger = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(herder_trigger)

reap_plan = herder_trigger.reap_plan
parse_pane_id = herder_trigger.parse_pane_id
SPAWN_TTL = herder_trigger.SPAWN_TTL_SECONDS
PANE_TTL = herder_trigger.PANE_TTL_SECONDS

NOW = 1_000_000.0


def _state(**panes):
    """panes: pane_id -> (task_id, age_seconds)."""
    return {pane: {"task_id": task, "at": NOW - age} for pane, (task, age) in panes.items()}


class TestKeepsWorkingPanes:
    def test_a_live_claim_is_kept(self):
        keep, kill = reap_plan(_state(**{"w1:pA": ("t1", 60)}), set(), {"t1"}, NOW)
        assert list(keep) == ["w1:pA"]
        assert kill == []

    def test_a_fresh_spawn_that_has_not_claimed_yet_is_kept(self):
        """The gap between spawning and the first claim is normal startup, and
        the task is still WAITING throughout it."""
        keep, kill = reap_plan(_state(**{"w1:pA": ("t1", 30)}), {"t1"}, set(), NOW)
        assert list(keep) == ["w1:pA"]
        assert kill == []

    def test_two_panes_on_one_task_are_tracked_independently(self):
        """A revision round re-claims the same task_id. Keying state by task
        would make the round-2 pane overwrite the round-1 entry and orphan it."""
        state = _state(**{"w1:pA": ("t1", 3000), "w1:pB": ("t1", 60)})
        keep, kill = reap_plan(state, set(), {"t1"}, NOW)
        assert list(keep) == ["w1:pB"], "the working round-2 pane survives"
        assert kill == ["w1:pA"], "the stale round-1 pane is closed"


class TestReapsFinishedPanes:
    def test_a_finished_claim_is_reaped(self):
        """Neither waiting nor live: the herder completed or released, so the
        pane is idle at a prompt forever unless something closes it."""
        keep, kill = reap_plan(_state(**{"w1:pA": ("t1", 120)}), set(), set(), NOW)
        assert keep == {}
        assert kill == ["w1:pA"]

    def test_success_and_failure_are_reaped_alike(self):
        """Decided deliberately: the workspace ends up empty either way, and a
        failed run is read back from get_agent_log rather than a live pane."""
        keep, kill = reap_plan(_state(**{"ok": ("t1", 120), "bad": ("t2", 120)}), set(), set(), NOW)
        assert keep == {}
        assert sorted(kill) == ["bad", "ok"]

    def test_a_spawn_that_never_claimed_is_reaped_after_its_window(self):
        state = _state(**{"w1:pA": ("t1", SPAWN_TTL + 1)})
        _keep, kill = reap_plan(state, {"t1"}, set(), NOW)
        assert kill == ["w1:pA"]

    def test_a_hung_pane_is_reaped_even_while_its_claim_reads_live(self):
        """The backstop. A herder that claimed and then wedged keeps its agent
        row 'running', so no other rule fires. PANE_TTL matches the engine's own
        herder_work_timeout_seconds, the point at which it already presumes the
        worker gone."""
        state = _state(**{"w1:pA": ("t1", PANE_TTL + 1)})
        keep, kill = reap_plan(state, set(), {"t1"}, NOW)
        assert keep == {}
        assert kill == ["w1:pA"]

    def test_the_ttl_backstop_is_the_engines_own_threshold(self):
        """Reaping sooner than the engine re-offers the task would hand live
        work to the metered path."""
        from minions.config import Config

        assert Config().herder_work_timeout_seconds == PANE_TTL


class TestLegacyState:
    def test_a_pre_reaper_entry_is_dropped_not_crashed_on(self):
        """Old state was {task_id: timestamp}. Those entries name no pane, and
        herdr uniquifies spawn names so one cannot be derived — carrying them
        forever would wedge the MAX_HERDERS budget."""
        keep, kill = reap_plan({"t1": NOW - 60}, set(), set(), NOW)
        assert keep == {}
        assert kill == [], "nothing to kill: there is no pane id to kill with"

    def test_legacy_and_new_entries_coexist(self):
        state = {"t1": NOW - 60, "w1:pA": {"task_id": "t2", "at": NOW - 60}}
        keep, kill = reap_plan(state, set(), {"t2"}, NOW)
        assert list(keep) == ["w1:pA"]
        assert kill == []


class TestParsePaneId:
    """`substrate.sh spawn --print` emits herdr's whole JSON envelope, not an id.

    Shape verified against a real spawn on 2026-08-21; `substrate.sh kill` wants
    the pane_id from inside it.
    """

    ENVELOPE = (
        '{"id":"cli:agent:start","result":{"agent":{"agent_status":"unknown","cwd":"/home/x",'
        '"name":"minions-herd-abc","pane_id":"w11:pA","tab_id":"w11:t1","workspace_id":"w11"},'
        '"argv":["sh","-c","true"],"type":"agent_started"}}'
    )

    def test_extracts_the_pane_id(self):
        assert parse_pane_id(self.ENVELOPE) == "w11:pA"

    def test_tolerates_surrounding_whitespace(self):
        assert parse_pane_id(f"\n  {self.ENVELOPE}  \n") == "w11:pA"

    @pytest.mark.parametrize("bad", ["", "not json", "{}", '{"result":{}}', '{"result":{"agent":{}}}'])
    def test_unparseable_output_yields_empty_not_an_exception(self, bad):
        """The caller turns "" into a loud log line. A raise here would kill the
        tick, and a fabricated id would make the reaper close someone else's
        pane."""
        assert parse_pane_id(bad) == ""
