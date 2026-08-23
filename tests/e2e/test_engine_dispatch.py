"""The dispatch layer itself — `JobEngine._advance()` and the LangGraph router.

Why this file exists: every other e2e test calls the phase handlers directly
(`dev.launch_spec_analyst`, `dev.launch_arbiter`, `dev.launch_engineers`), so
none of them ever reach `_advance()`. That left `advance_job_via_graph` and
`route_after_phase` with **zero** integration coverage while
`use_langgraph_engine` defaults to True in production — the graph engine is what
orchestrates real jobs, and nothing here exercised it.

That gap is not academic. It is the exact shape of the bug fixed in 0.8.39: the
router re-entered waiting-state nodes until langgraph's recursion limit, and
`_advance()` caught the error and fell back to the legacy dispatcher. Jobs still
completed, so the only symptom was a warning — the graph engine was doing none
of the work for two days and every test stayed green.

So the assertions here are deliberately about the *mechanism*, not the outcome:
that the graph ran at all, and that it yielded rather than spun. A test that
only checked the final job status would pass identically in both worlds.

Scope note — which waiting states belong to which graph:
`route_after_start` maps dev jobs to nodes for spec_received..dev_in_progress,
merged, deploying, deployed. It deliberately does NOT map `pr_open` or
`review_in_progress`, and an unmapped status routes to `fail`. That is currently
harmless because neither state is reachable for a dev job: `JobStatus.PR_OPEN`
has no writer anywhere in the tree, and `REVIEW_IN_PROGRESS` is written only by
`review.py` and `cli.py` for *review*-type jobs, which run the review graph where
it maps to `check_review`. The states are legal per `JOB_TRANSITIONS` but
unreachable in code — so each case below is exercised against the graph that
actually owns it, and a dev job is never parked somewhere only a review job goes.
"""

import logging

import pytest

from minions.core.models import JobStatus
from minions.engine import job_graph

# (label, job kind, waiting status) — a waiting state means work is in flight
# elsewhere, so a node that runs against it legitimately changes nothing. That
# is precisely the case the pre-0.8.39 router mistook for "route again".
WAITING_CASES = [
    ("dev-dev_in_progress", "dev", JobStatus.DEV_IN_PROGRESS),
    # dev-deploying left this list in 0.8.53: DEPLOYING stopped being a waiting
    # state when the deploy leg was retired — check_deployed now HEALS a parked
    # job forward (deployment is delegated to each repo's own CD), and healing
    # is progress, not a yield. test_deploying_is_healed_not_parked below owns
    # that contract at this same dispatch layer.
    ("review-review_in_progress", "review", JobStatus.REVIEW_IN_PROGRESS),
]

# Legal routes to each dev waiting state. The DB enforces JOB_TRANSITIONS, so a
# job cannot be teleported into one — walking the real path is also a small free
# assertion that the route still exists.
DEV_ROUTE_TO: dict[JobStatus, list[JobStatus]] = {
    JobStatus.DEV_IN_PROGRESS: [
        JobStatus.SPEC_READY,
        JobStatus.TASKS_CREATED,
        JobStatus.DEV_IN_PROGRESS,
    ],
    JobStatus.DEPLOYING: [
        JobStatus.SPEC_READY,
        JobStatus.TASKS_CREATED,
        JobStatus.DEV_IN_PROGRESS,
        JobStatus.MERGED,
        JobStatus.DEPLOYING,
    ],
}


async def _job_in_state(db, kind: str, status: JobStatus):
    """A real job walked down its legal path into a waiting state."""
    if kind == "review":
        job, _task = await db.create_review_job("proj", "https://example.test/mr/1", "1")
        assert await db.update_job_status(job.id, status)
        return await db.get_job(job.id)

    job = await db.create_job(spec="dispatch-layer coverage")
    for step in DEV_ROUTE_TO[status]:
        assert await db.update_job_status(job.id, step), f"could not move job to {step}"
    return await db.get_job(job.id)


@pytest.fixture
def graph_spy(monkeypatch):
    """Record whether the graph engine ran, and what the router decided."""
    seen = {"advances": 0, "routes": []}

    original_route = job_graph.route_after_phase

    def traced(state):
        decision = original_route(state)
        seen["routes"].append(str(decision))
        return decision

    monkeypatch.setattr(job_graph, "route_after_phase", traced)

    original_advance = job_graph.advance_job_via_graph

    async def counted(engine, job, checkpointer=None):
        seen["advances"] += 1
        return await original_advance(engine, job, checkpointer=checkpointer)

    # Patch both the definition and the name job_engine bound at import time.
    monkeypatch.setattr(job_graph, "advance_job_via_graph", counted)
    monkeypatch.setattr("minions.engine.job_engine.advance_job_via_graph", counted)

    return seen


def _fallbacks(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if "falling back" in r.getMessage()]


@pytest.mark.parametrize(("label", "kind", "status"), WAITING_CASES, ids=[c[0] for c in WAITING_CASES])
async def test_advance_routes_through_the_graph_engine(e2e_engine, db, graph_spy, label, kind, status):
    """`_advance()` reaches LangGraph rather than silently using the legacy path.

    Guards the subsystem as a whole: if `use_langgraph_engine` were flipped off,
    the import removed, or the call site refactored away, every other test would
    still pass and this one would not.
    """
    job = await _job_in_state(db, kind, status)

    await e2e_engine._advance(job)

    assert graph_spy["advances"] == 1, "LangGraph engine never ran — dispatch used the legacy path"


@pytest.mark.parametrize(("label", "kind", "status"), WAITING_CASES, ids=[c[0] for c in WAITING_CASES])
async def test_waiting_state_yields_instead_of_spinning(e2e_engine, db, graph_spy, caplog, label, kind, status):
    """The 0.8.39 fix: an unchanged status ends the graph run, and no fallback fires.

    Pre-fix this raised `GraphRecursionError` after re-entering the node it had
    just left, which `_advance()` swallowed into a warning. Asserting on the
    absence of that warning is what separates "fixed" from "failing quietly".
    """
    job = await _job_in_state(db, kind, status)

    with caplog.at_level(logging.WARNING, logger="minions.engine.job_engine"):
        await e2e_engine._advance(job)

    assert not _fallbacks(caplog), f"graph degraded to the legacy dispatcher: {_fallbacks(caplog)}"

    # The job must still be parked where it was; yielding is not progress.
    after = await db.get_job(job.id)
    assert after.status == status

    assert graph_spy["routes"], "router never consulted"
    assert graph_spy["routes"][-1] == "__end__", f"router did not yield; returned {graph_spy['routes'][-1]!r}"


async def test_deploying_is_healed_not_parked(e2e_engine, db, caplog):
    """Since the deploy leg's retirement, DEPLOYING is a legacy state: nothing
    produces it any more, and a job found there was left by the pre-0.8.53
    monitor that could never conclude. The dispatch layer must advance it —
    through DEPLOYED to DONE — rather than treat it as work in flight."""
    job = await _job_in_state(db, "dev", JobStatus.DEPLOYING)

    with caplog.at_level(logging.WARNING, logger="minions.engine.job_engine"):
        await e2e_engine._advance(job)

    assert not _fallbacks(caplog), f"graph degraded to the legacy dispatcher: {_fallbacks(caplog)}"
    after = await db.get_job(job.id)
    assert after.status == JobStatus.DONE, f"a legacy DEPLOYING job must heal forward, not park (got {after.status})"
    healed = [e for e in await db.get_events(job.id) if e.get("event_type") == "deploy_healed"]
    assert healed, "the heal must leave a trace"


@pytest.mark.parametrize(("label", "kind", "status"), WAITING_CASES, ids=[c[0] for c in WAITING_CASES])
async def test_these_tests_go_red_without_the_fix(e2e_engine, db, monkeypatch, caplog, label, kind, status):
    """Mutation check: damage the fix, and the assertions above must fail.

    `prev_status` is the whole mechanism — the router compares it against the
    refreshed status to tell a no-op from progress. Neutralising it reproduces
    the pre-0.8.39 router exactly, without reimplementing it. A green run here
    would mean the tests above cannot detect the regression they exist for, and
    a fail-open guard is observationally identical to a working one.
    """
    original_refresh = job_graph._refresh_state

    async def refresh_without_prev_status(state, *args, **kwargs):
        out = await original_refresh(state, *args, **kwargs)
        if isinstance(out, dict):
            return {**out, "prev_status": None}
        return out

    monkeypatch.setattr(job_graph, "_refresh_state", refresh_without_prev_status)

    job = await _job_in_state(db, kind, status)

    with caplog.at_level(logging.WARNING, logger="minions.engine.job_engine"):
        await e2e_engine._advance(job)

    assert _fallbacks(caplog), "damaged router did NOT spin — these tests cannot detect the 0.8.39 regression"
