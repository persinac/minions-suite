"""LangGraph-based job orchestration graph.

Replaces the `_advance()` dispatcher in job_engine.py with an explicit
StateGraph. Each job phase is a node, with conditional edges for routing.

The graph is a *view* over the DB — nodes read/write DB state and return
routing hints. The DB remains the source of truth.
"""

import logging
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from ..core.models import JobStatus

logger = logging.getLogger(__name__)


class JobGraphState(TypedDict):
    """State for the job orchestration graph.

    Nodes read from DB for current truth and write back via engine helpers.
    This state carries routing context between nodes.
    """

    job_id: str
    job_status: str
    job_type: str  # "dev" or "review"
    tasks: list[dict]  # task snapshots: [{id, service, status, agent_role, ...}]
    active_agents: list[dict]  # running agents: [{id, role, status}]
    current_phase: str  # routing hint for conditional edges
    error: str | None

    # The status the last node was ENTERED with. `route_after_phase` compares it
    # against the refreshed status to tell "this node advanced the job" from
    # "this node ran and nothing changed". Without it the router cannot see a
    # waiting state and re-enters the same node until the recursion limit trips.
    prev_status: str | None

    # Engine reference (not serializable — excluded from checkpointing)
    engine: Any  # JobEngine instance


# A real advance visits a handful of nodes — spec_analysis through completion is
# nine. Anything approaching this ceiling is a routing bug, not a long job.
#
# langgraph 1.x defaults recursion_limit to 10000 (_internal/_config.py), and
# nothing here used to override it. That turned the missing yield below into
# ~10k iterations of get_job + phase function + refresh — order 30k DB round
# trips — blocking the poll loop before it finally raised. A tight ceiling makes
# the same class of bug fail fast and loudly instead.
GRAPH_RECURSION_LIMIT = 25


# ---------------------------------------------------------------------------
# Helper: refresh state from DB
# ---------------------------------------------------------------------------


async def _refresh_state(engine, state: JobGraphState) -> dict:
    """Read current job + tasks from DB and return state update.

    Takes the whole state rather than just the id so it can record `prev_status`
    — the status the node was entered with. That is what lets `route_after_phase`
    distinguish progress from a no-op; see JobGraphState.prev_status.
    """
    job_id = state["job_id"]
    entry_status = state.get("job_status")

    job = await engine.db.get_job(job_id)
    if not job:
        return {"error": f"Job {job_id} not found", "current_phase": "fail"}

    tasks = await engine.db.get_tasks(job_id)
    task_dicts = [
        {
            "id": t.id,
            "service": t.service,
            "status": str(t.status) if hasattr(t.status, "value") else t.status,
            "agent_role": str(t.agent_role) if hasattr(t.agent_role, "value") else t.agent_role,
            "revision_count": getattr(t, "revision_count", 0),
            "attempt": getattr(t, "attempt", 1),
            "max_attempts": getattr(t, "max_attempts", 3),
            "review_status": getattr(t, "review_status", None),
            "pr_url": getattr(t, "pr_url", None),
            "verdict": getattr(t, "verdict", None),
        }
        for t in tasks
    ]

    agents = await engine.db.get_agents_for_job(job_id)
    running = [{"id": a.id, "role": str(a.role), "status": a.status} for a in agents if a.status == "running"]

    return {
        "job_status": str(job.status) if hasattr(job.status, "value") else job.status,
        "prev_status": entry_status,
        "tasks": task_dicts,
        "active_agents": running,
        "error": None,
    }


# ---------------------------------------------------------------------------
# Graph nodes — thin wrappers around existing engine functions
# ---------------------------------------------------------------------------


async def spec_analysis_node(state: JobGraphState) -> dict:
    """Launch the spec analyst agent."""
    from . import dev

    engine = state["engine"]
    job = await engine.db.get_job(state["job_id"])

    try:
        await dev.launch_spec_analyst(engine, job)
    except Exception as e:
        logger.error("spec_analysis_node failed: %s", e, exc_info=True)
        return {"error": str(e)[:500], "current_phase": "fail"}

    refreshed = await _refresh_state(engine, state)
    refreshed["current_phase"] = "route_after_spec"
    return refreshed


async def task_decomposition_node(state: JobGraphState) -> dict:
    """Launch the arbiter to decompose the spec into tasks."""
    from . import dev

    engine = state["engine"]
    job = await engine.db.get_job(state["job_id"])

    try:
        await dev.launch_arbiter(engine, job)
    except Exception as e:
        logger.error("task_decomposition_node failed: %s", e, exc_info=True)
        return {"error": str(e)[:500], "current_phase": "fail"}

    refreshed = await _refresh_state(engine, state)
    refreshed["current_phase"] = "route_after_arbiter"
    return refreshed


async def engineer_dispatch_node(state: JobGraphState) -> dict:
    """Launch engineers for pending tasks (one per service)."""
    from . import dev

    engine = state["engine"]
    job = await engine.db.get_job(state["job_id"])

    try:
        await dev.launch_engineers(engine, job)
    except Exception as e:
        logger.error("engineer_dispatch_node failed: %s", e, exc_info=True)
        return {"error": str(e)[:500], "current_phase": "fail"}

    refreshed = await _refresh_state(engine, state)
    refreshed["current_phase"] = "route_after_engineer"
    return refreshed


async def manage_dev_node(state: JobGraphState) -> dict:
    """Manage dev tasks — handles PR_OPEN, IN_REVIEW, revision cycles."""
    from . import dev

    engine = state["engine"]
    job = await engine.db.get_job(state["job_id"])

    try:
        await dev.manage_dev_tasks(engine, job)
    except Exception as e:
        logger.error("manage_dev_node failed: %s", e, exc_info=True)
        return {"error": str(e)[:500], "current_phase": "fail"}

    refreshed = await _refresh_state(engine, state)
    refreshed["current_phase"] = "route_after_manage_dev"
    return refreshed


async def review_dispatch_node(state: JobGraphState) -> dict:
    """Launch review tasks for a review-type job."""
    from . import review

    engine = state["engine"]
    job = await engine.db.get_job(state["job_id"])

    try:
        await review.launch_review_tasks(engine, job)
    except Exception as e:
        logger.error("review_dispatch_node failed: %s", e, exc_info=True)
        return {"error": str(e)[:500], "current_phase": "fail"}

    refreshed = await _refresh_state(engine, state)
    refreshed["current_phase"] = "route_after_review_dispatch"
    return refreshed


async def check_review_node(state: JobGraphState) -> dict:
    """Check if review tasks are complete."""
    from . import review

    engine = state["engine"]
    job = await engine.db.get_job(state["job_id"])

    try:
        await review.check_review_tasks(engine, job)
    except Exception as e:
        logger.error("check_review_node failed: %s", e, exc_info=True)
        return {"error": str(e)[:500], "current_phase": "fail"}

    refreshed = await _refresh_state(engine, state)
    refreshed["current_phase"] = "route_after_check_review"
    return refreshed


async def deploy_node(state: JobGraphState) -> dict:
    """Launch the deploy monitor."""
    from . import deploy

    engine = state["engine"]
    job = await engine.db.get_job(state["job_id"])

    try:
        await deploy.launch_deploy_monitor(engine, job)
    except Exception as e:
        logger.error("deploy_node failed: %s", e, exc_info=True)
        return {"error": str(e)[:500], "current_phase": "fail"}

    refreshed = await _refresh_state(engine, state)
    refreshed["current_phase"] = "route_after_deploy"
    return refreshed


async def check_deploy_node(state: JobGraphState) -> dict:
    """Check if deployment is complete."""
    from . import deploy

    engine = state["engine"]
    job = await engine.db.get_job(state["job_id"])

    try:
        await deploy.check_deployed(engine, job)
    except Exception as e:
        logger.error("check_deploy_node failed: %s", e, exc_info=True)
        return {"error": str(e)[:500], "current_phase": "fail"}

    refreshed = await _refresh_state(engine, state)
    refreshed["current_phase"] = "route_after_check_deploy"
    return refreshed


async def completion_node(state: JobGraphState) -> dict:
    """Mark job as done and clean up checkpoints."""
    engine = state["engine"]

    try:
        await engine.db.update_job_status(state["job_id"], JobStatus.DONE)
        logger.info("Job %s completed successfully via graph!", state["job_id"])
    except Exception as e:
        logger.error("completion_node failed: %s", e, exc_info=True)
        return {"error": str(e)[:500], "current_phase": "fail"}

    return {"job_status": "done", "current_phase": "done"}


async def fail_node(state: JobGraphState) -> dict:
    """Mark job as failed."""
    engine = state["engine"]
    error = state.get("error", "Unknown error")

    try:
        await engine.db.update_job_status(state["job_id"], JobStatus.FAILED, error=error)
        logger.error("Job %s failed via graph: %s", state["job_id"], error)
    except Exception as e:
        logger.error("fail_node itself failed: %s", e, exc_info=True)

    return {"job_status": "failed", "current_phase": "done"}


# ---------------------------------------------------------------------------
# Conditional routing functions
# ---------------------------------------------------------------------------


def route_after_start(state: JobGraphState) -> str:
    """Route from START based on job status and type."""
    status = state["job_status"]
    job_type = state.get("job_type", "dev")

    if state.get("error"):
        return "fail"

    # Review jobs have a simpler path
    if job_type == "review":
        if status == "tasks_created":
            return "review_dispatch"
        if status == "review_in_progress":
            return "check_review"
        if status in ("done", "failed"):
            return END
        return "fail"

    # Dev jobs
    status_to_node = {
        "spec_received": "spec_analysis",
        "spec_ready": "task_decomposition",
        "tasks_created": "engineer_dispatch",
        "dev_in_progress": "manage_dev",
        "merged": "deploy",
        "deploying": "check_deploy",
        "deployed": "completion",
    }

    if status in ("done", "failed", "no_work_needed"):
        return END

    node = status_to_node.get(status)
    if node:
        return node

    logger.warning("Unknown job status for routing: %s", status)
    return "fail"


def route_after_phase(state: JobGraphState) -> str:
    """Generic router — re-enters the start router after any phase completes.

    This allows the graph to naturally follow the job through its lifecycle
    by re-reading the DB state after each node.

    It yields when a node did not move the job, which is what keeps
    `advance_job_via_graph` to the "one step" its name promises.
    """
    if state.get("error"):
        return "fail"

    status = state["job_status"]

    if status in ("done", "failed", "no_work_needed"):
        return END

    # A node that ran without changing the job status has nothing further to do
    # RIGHT NOW. dev_in_progress, review_in_progress and deploying are waiting
    # states: engineers, reviewers or a deploy are still in flight, and
    # "unchanged" is the correct outcome rather than a failure. Re-routing on an
    # unchanged status sends the job straight back into the node it just left,
    # which spun until langgraph's recursion limit and was only survivable
    # because _advance() catches the error and falls back to the legacy
    # dispatcher -- so the graph looked healthy while doing none of the work.
    #
    # Yield instead: the poll loop will re-enter the graph on its next tick,
    # by which time the awaited work may actually have progressed.
    prev = state.get("prev_status")
    if prev is not None and prev == status:
        logger.debug(
            "Job %s unchanged at %s after %s — yielding to poll loop",
            state.get("job_id"),
            status,
            state.get("current_phase"),
        )
        return END

    # Re-route based on current status (DB was updated by the node)
    return route_after_start(state)


# ---------------------------------------------------------------------------
# Graph builders
# ---------------------------------------------------------------------------


def build_job_graph(checkpointer=None):
    """Build the dev job orchestration graph.

    Nodes wrap existing engine functions. After each node, the graph re-reads
    DB state and routes to the next appropriate node.
    """
    graph = StateGraph(JobGraphState)

    # Add all nodes
    graph.add_node("spec_analysis", spec_analysis_node)
    graph.add_node("task_decomposition", task_decomposition_node)
    graph.add_node("engineer_dispatch", engineer_dispatch_node)
    graph.add_node("manage_dev", manage_dev_node)
    graph.add_node("review_dispatch", review_dispatch_node)
    graph.add_node("check_review", check_review_node)
    graph.add_node("deploy", deploy_node)
    graph.add_node("check_deploy", check_deploy_node)
    graph.add_node("completion", completion_node)
    graph.add_node("fail", fail_node)

    # START routes to the appropriate node based on current job status
    graph.add_conditional_edges(START, route_after_start)

    # After each node, re-route based on updated DB state
    for node_name in [
        "spec_analysis",
        "task_decomposition",
        "engineer_dispatch",
        "manage_dev",
        "review_dispatch",
        "check_review",
        "deploy",
        "check_deploy",
    ]:
        graph.add_conditional_edges(node_name, route_after_phase)

    # Terminal nodes
    graph.add_edge("completion", END)
    graph.add_edge("fail", END)

    return graph.compile(checkpointer=checkpointer)


def build_review_job_graph(checkpointer=None):
    """Build a simplified graph for review-only jobs.

    review_dispatch → check_review → completion/fail
    """
    graph = StateGraph(JobGraphState)

    graph.add_node("review_dispatch", review_dispatch_node)
    graph.add_node("check_review", check_review_node)
    graph.add_node("completion", completion_node)
    graph.add_node("fail", fail_node)

    # START routes based on job status
    graph.add_conditional_edges(START, route_after_start)

    # After dispatch, check if reviews are done
    graph.add_conditional_edges("review_dispatch", route_after_phase)
    graph.add_conditional_edges("check_review", route_after_phase)

    # Terminal
    graph.add_edge("completion", END)
    graph.add_edge("fail", END)

    return graph.compile(checkpointer=checkpointer)


# ---------------------------------------------------------------------------
# Graph execution helper
# ---------------------------------------------------------------------------


async def advance_job_via_graph(engine, job, checkpointer=None) -> None:
    """Advance a job one step using the LangGraph orchestration graph.

    Called from JobEngine._advance() when USE_LANGGRAPH_ENGINE=true.
    """
    job_type = getattr(job, "job_type", "dev") or "dev"

    if job_type == "review":
        graph = build_review_job_graph(checkpointer=checkpointer)
    else:
        graph = build_job_graph(checkpointer=checkpointer)

    status = str(job.status) if hasattr(job.status, "value") else job.status

    initial_state: JobGraphState = {
        "job_id": job.id,
        "job_status": status,
        "job_type": job_type,
        "tasks": [],
        "active_agents": [],
        "current_phase": "start",
        "error": None,
        "prev_status": None,
        "engine": engine,
    }

    config = {
        "configurable": {"thread_id": job.id},
        "recursion_limit": GRAPH_RECURSION_LIMIT,
    }

    await graph.ainvoke(initial_state, config)


async def resume_from_checkpoint(engine, job_id: str, checkpointer) -> bool:
    """Attempt to resume a job from its last checkpoint.

    Returns True if a checkpoint was found and resumed, False otherwise.
    """
    if not checkpointer:
        return False

    config = {
        "configurable": {"thread_id": job_id},
        "recursion_limit": GRAPH_RECURSION_LIMIT,
    }

    job = await engine.db.get_job(job_id)
    if not job:
        return False

    job_type = getattr(job, "job_type", "dev") or "dev"
    if job_type == "review":
        graph = build_review_job_graph(checkpointer=checkpointer)
    else:
        graph = build_job_graph(checkpointer=checkpointer)

    try:
        state = await graph.aget_state(config)
        if state and state.values:
            logger.info("Resuming job %s from checkpoint", job_id)
            # Update engine reference (not persisted in checkpoint)
            await graph.ainvoke(None, config)
            return True
    except Exception as e:
        logger.warning("Could not resume job %s from checkpoint: %s", job_id, e)

    return False
