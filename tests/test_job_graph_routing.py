"""Routing tests for the LangGraph job orchestration graph.

`job_graph.py` is default-on (`use_langgraph_engine=True`) and had no test file
at all. These cover the routers, which are pure functions over state — no DB.

The bug that motivated them: `route_after_phase` re-routed on the refreshed job
status with no notion of "the node changed nothing". For a job in a waiting
state (engineers/reviewers/deploy still in flight) that sent it straight back
into the node it had just left, spinning until langgraph's recursion limit --
10000 by default in langgraph 1.x, so ~30k DB round trips inside the poll loop
before it raised. `_advance()` caught the error and fell back to the legacy
dispatcher, so jobs still completed and the graph looked healthy while doing
none of the work.
"""

from types import SimpleNamespace

import pytest
from langgraph.graph import END

from minions.engine.job_graph import (
    GRAPH_RECURSION_LIMIT,
    _refresh_state,
    route_after_phase,
    route_after_start,
)

# Status -> node, mirroring route_after_start's dev mapping. Kept here rather
# than imported so a change to the mapping has to be made deliberately in two
# places instead of silently agreeing with itself.
DEV_WAITING_STATES = [
    ("dev_in_progress", "manage_dev"),
    ("deploying", "check_deploy"),
]

DEV_ADVANCING = [
    ("spec_received", "spec_analysis"),
    ("spec_ready", "task_decomposition"),
    ("tasks_created", "engineer_dispatch"),
    ("merged", "deploy"),
    ("deployed", "completion"),
]

TERMINAL = ["done", "failed", "no_work_needed"]


def _state(status, prev=None, job_type="dev", error=None):
    return {
        "job_id": "job-1",
        "job_status": status,
        "job_type": job_type,
        "tasks": [],
        "active_agents": [],
        "current_phase": "test",
        "error": error,
        "prev_status": prev,
        "engine": None,
    }


# ---------------------------------------------------------------------------
# The regression: an unchanged status must yield, not re-enter the node
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status,node", DEV_WAITING_STATES)
def test_unchanged_waiting_status_yields_instead_of_relooping(status, node):
    """The waiting states are where 'nothing changed' is the CORRECT outcome."""
    # Sanity: on entry this status really does route into the node we claim.
    assert route_after_start(_state(status)) == node

    # After the node ran and left the status alone, the router must yield.
    assert route_after_phase(_state(status, prev=status)) == END


def test_review_job_unchanged_status_yields():
    assert route_after_start(_state("review_in_progress", job_type="review")) == "check_review"
    assert route_after_phase(_state("review_in_progress", prev="review_in_progress", job_type="review")) == END


@pytest.mark.parametrize("status,node", DEV_ADVANCING)
def test_changed_status_still_advances(status, node):
    """Yielding must not cost us progress: a status that MOVED keeps routing."""
    assert route_after_phase(_state(status, prev="something_else")) == node


def test_first_entry_with_no_prev_status_still_routes():
    """prev_status is None on the first pass. If that yielded, nothing would run."""
    assert route_after_phase(_state("spec_received", prev=None)) == "spec_analysis"


def test_no_dev_status_can_reenter_its_own_node_unchanged():
    """Computed from the mapping, so a NEW status cannot reintroduce the spin.

    Adding a state to route_after_start without thinking about the waiting case
    is exactly how this bug got in; this fails for any status that would route
    back into itself on a no-op.
    """
    for status, _node in DEV_WAITING_STATES + DEV_ADVANCING:
        assert route_after_phase(_state(status, prev=status)) == END, f"{status} re-enters its own node when unchanged"


# ---------------------------------------------------------------------------
# Terminal and error routing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", TERMINAL)
def test_terminal_status_ends(status):
    assert route_after_phase(_state(status)) == END
    assert route_after_start(_state(status)) == END


@pytest.mark.parametrize("status", TERMINAL)
def test_terminal_status_ends_even_when_unchanged(status):
    """Terminality must win over the yield, and both reach END anyway."""
    assert route_after_phase(_state(status, prev=status)) == END


def test_error_routes_to_fail_before_anything_else():
    assert route_after_phase(_state("dev_in_progress", prev="dev_in_progress", error="boom")) == "fail"
    assert route_after_start(_state("spec_received", error="boom")) == "fail"


def test_unknown_status_fails_rather_than_looping():
    assert route_after_start(_state("not_a_real_status")) == "fail"


# ---------------------------------------------------------------------------
# _refresh_state must actually supply what the router depends on
# ---------------------------------------------------------------------------


class _FakeDB:
    def __init__(self, status):
        self._status = status

    async def get_job(self, job_id):
        return SimpleNamespace(id=job_id, status=self._status, job_type="dev")

    async def get_tasks(self, job_id):
        return []

    async def get_agents_for_job(self, job_id):
        return []


@pytest.mark.asyncio
async def test_refresh_state_records_entry_status_as_prev():
    """The router's yield is only as good as this field being populated."""
    engine = SimpleNamespace(db=_FakeDB("dev_in_progress"))
    out = await _refresh_state(engine, _state("dev_in_progress"))

    assert out["prev_status"] == "dev_in_progress"
    assert out["job_status"] == "dev_in_progress"
    # and the pair round-trips into a yield
    assert route_after_phase({**_state("x"), **out}) == END


@pytest.mark.asyncio
async def test_refresh_state_prev_differs_when_node_advanced_the_job():
    engine = SimpleNamespace(db=_FakeDB("merged"))
    out = await _refresh_state(engine, _state("dev_in_progress"))

    assert out["prev_status"] == "dev_in_progress"
    assert out["job_status"] == "merged"
    assert route_after_phase({**_state("x"), **out}) == "deploy"


@pytest.mark.asyncio
async def test_refresh_state_missing_job_is_an_error_not_a_loop():
    class _NoJobDB(_FakeDB):
        async def get_job(self, job_id):
            return None

    engine = SimpleNamespace(db=_NoJobDB("whatever"))
    out = await _refresh_state(engine, _state("dev_in_progress"))

    assert out["error"]
    assert route_after_phase({**_state("dev_in_progress"), **out}) == "fail"


# ---------------------------------------------------------------------------
# The backstop
# ---------------------------------------------------------------------------


def test_recursion_limit_is_far_below_the_langgraph_default():
    """10000 is langgraph 1.x's default and is what let the spin run so long.

    The ceiling only needs to clear the longest real path (spec_analysis ->
    completion is nine nodes); anything near it is a routing bug.
    """
    assert 9 < GRAPH_RECURSION_LIMIT < 100
