"""A job walked from intake to dispatch by scripted agents, asserting every stop.

The value here is in the intermediate states, not the destination. A test that
checks only the final status passes just as happily when a job teleports --
skipping `spec_ready`, never recording an event, leaving the analyst's task
`in_progress` forever -- because the end state looks identical either way. Each
phase below asserts the status, the task bookkeeping, and the event trail.

Scope note: coverage stops at dispatch. `report_pr` shells out to the `gh` CLI to
verify a self-reported PR exists (`_verify_reported_pr` in server/mcp.py), so the
PR and merge legs need a second fake and live in test_pr_leg.py, clearly labelled.
Everything in this file runs with no network at all.
"""

import asyncio

from minions.core.models import JobStatus, TaskStatus
from minions.engine import dev

from .conftest import Call, turn

REFINED_SPEC = """# Refined: add an export endpoint

Add `GET /export` returning the current dataset as CSV.

## Acceptance criteria
- Endpoint returns 200 with a CSV body
- Covered by a test

## Assumptions
1. "the dataset" — read as the orders table, the only dataset in the repo.
2. No pagination — the ticket names none and the existing exporters have none.
"""


def _script_the_happy_path(scripted_llm, service: str = "api") -> None:
    scripted_llm.script("spec_analyst", turn(Call("submit_refined_spec", {"spec": REFINED_SPEC})))
    scripted_llm.script(
        "arbiter",
        turn(
            Call(
                "create_task",
                {
                    "title": "Add export endpoint",
                    "description": "Implement GET /export with a test.",
                    "service": service,
                    "agent_role": "backend_engineer",
                },
            )
        ),
        turn(Call("mark_tasks_created", {})),
    )


async def _drain(engine) -> None:
    """Wait for engineers spawned as background tasks."""
    pending = list(engine._background_tasks)
    if pending:
        await asyncio.wait(pending, timeout=10)


class TestIntake:
    async def test_a_new_job_starts_at_spec_received(self, db):
        job = await db.create_job("Add an export endpoint")
        assert job.status == JobStatus.SPEC_RECEIVED

    async def test_the_analyst_advances_it_and_closes_its_own_task(self, db, e2e_engine, scripted_llm):
        _script_the_happy_path(scripted_llm)
        job = await db.create_job("Add an export endpoint")

        await dev.launch_spec_analyst(e2e_engine, job)

        assert (await db.get_job(job.id)).status == JobStatus.SPEC_READY
        spec_tasks = [t for t in await db.get_tasks(job.id) if t.service == "_spec"]
        assert len(spec_tasks) == 1
        assert spec_tasks[0].status == TaskStatus.DONE, "the analyst's virtual task must not be left in_progress"

    async def test_it_records_an_event_trail(self, db, e2e_engine, scripted_llm):
        _script_the_happy_path(scripted_llm)
        job = await db.create_job("Add an export endpoint")

        await dev.launch_spec_analyst(e2e_engine, job)

        events = await db.get_events(job.id)
        kinds = {e["event_type"] for e in events}
        assert "agent_launched" in kinds
        assert "spec_refined" in kinds


class TestDecomposition:
    async def test_the_arbiter_creates_tasks_and_advances(self, db, e2e_engine, scripted_llm):
        _script_the_happy_path(scripted_llm)
        job = await db.create_job("Add an export endpoint")

        await dev.launch_spec_analyst(e2e_engine, job)
        await dev.launch_arbiter(e2e_engine, await db.get_job(job.id))

        assert (await db.get_job(job.id)).status == JobStatus.TASKS_CREATED
        real = [t for t in await db.get_tasks(job.id) if t.service not in ("_spec", "_arbiter")]
        assert len(real) == 1
        assert real[0].status == TaskStatus.PENDING

    async def test_signalling_zero_tasks_means_no_work_needed_not_failure(self, db, e2e_engine, scripted_llm):
        """Two zero-task outcomes exist, and the difference is deliberate.

        An arbiter that calls `mark_tasks_created` having created nothing is
        making a claim -- "I looked, there is nothing to do" -- and the job ends
        at NO_WORK_NEEDED, a terminal success. An arbiter that stops without
        signalling has told us nothing, and `launch_arbiter`'s own guard fails the
        job instead (covered below).

        Worth pinning because the two paths live in different files and neither
        mentions the other: the signalled case is resolved by `launch_engineers`
        (dev.py), the silent case by `launch_arbiter`.
        """
        scripted_llm.script("spec_analyst", turn(Call("submit_refined_spec", {"spec": REFINED_SPEC})))
        scripted_llm.script("arbiter", turn(Call("mark_tasks_created", {})))

        job = await db.create_job("Add an export endpoint")
        await dev.launch_spec_analyst(e2e_engine, job)
        await dev.launch_arbiter(e2e_engine, await db.get_job(job.id))
        assert (await db.get_job(job.id)).status == JobStatus.TASKS_CREATED

        await dev.launch_engineers(e2e_engine, await db.get_job(job.id))
        assert (await db.get_job(job.id)).status == JobStatus.NO_WORK_NEEDED

    async def test_an_arbiter_that_never_signals_fails_the_job(self, db, e2e_engine, scripted_llm):
        """Silence is not a conclusion. The job must not sit at spec_ready forever."""
        scripted_llm.script("spec_analyst", turn(Call("submit_refined_spec", {"spec": REFINED_SPEC})))
        scripted_llm.script("arbiter", turn())  # completes, creates nothing, says nothing

        job = await db.create_job("Add an export endpoint")
        await dev.launch_spec_analyst(e2e_engine, job)
        await dev.launch_arbiter(e2e_engine, await db.get_job(job.id))

        final = await db.get_job(job.id)
        assert final.status == JobStatus.FAILED
        assert "no tasks" in (final.error or "").lower()

    async def test_reserved_service_names_are_refused(self, db, e2e_engine, scripted_llm):
        """`_spec` and `_arbiter` are the engine's own bookkeeping rows.

        A task created against one would be filtered out of every "real tasks"
        query while still looking like progress to the arbiter that made it.
        """
        scripted_llm.script("spec_analyst", turn(Call("submit_refined_spec", {"spec": REFINED_SPEC})))
        scripted_llm.script(
            "arbiter",
            turn(Call("create_task", {"title": "x", "description": "y", "service": "_spec", "agent_role": "backend_engineer"})),
            turn(Call("mark_tasks_created", {})),
        )

        job = await db.create_job("Add an export endpoint")
        await dev.launch_spec_analyst(e2e_engine, job)
        await dev.launch_arbiter(e2e_engine, await db.get_job(job.id))

        real = [t for t in await db.get_tasks(job.id) if t.service not in ("_spec", "_arbiter")]
        assert real == [], "a reserved service name must not create a task"
        # Having created nothing, it lands where every zero-task job lands.
        await dev.launch_engineers(e2e_engine, await db.get_job(job.id))
        assert (await db.get_job(job.id)).status == JobStatus.NO_WORK_NEEDED


class TestDispatch:
    async def test_tasks_created_advances_to_dev_in_progress(self, db, e2e_engine, scripted_llm):
        _script_the_happy_path(scripted_llm)
        # The engineer does nothing; this asserts dispatch, not implementation.
        scripted_llm.script("backend_engineer", turn())

        job = await db.create_job("Add an export endpoint")
        await dev.launch_spec_analyst(e2e_engine, job)
        await dev.launch_arbiter(e2e_engine, await db.get_job(job.id))
        await dev.launch_engineers(e2e_engine, await db.get_job(job.id))
        await _drain(e2e_engine)

        assert (await db.get_job(job.id)).status == JobStatus.DEV_IN_PROGRESS

    async def test_dispatch_is_compare_and_swap(self, db, e2e_engine, scripted_llm):
        """Two engines must not both launch engineers for one job.

        The guard is a CAS on tasks_created -> dev_in_progress. Calling launch
        twice stands in for two engines racing; the second must find the job
        already moved and decline.
        """
        _script_the_happy_path(scripted_llm)
        scripted_llm.script("backend_engineer", turn())

        job = await db.create_job("Add an export endpoint")
        await dev.launch_spec_analyst(e2e_engine, job)
        await dev.launch_arbiter(e2e_engine, await db.get_job(job.id))

        staged = await db.get_job(job.id)
        await dev.launch_engineers(e2e_engine, staged)
        await dev.launch_engineers(e2e_engine, staged)  # same stale job object: the race
        await _drain(e2e_engine)

        agents = await db.get_agents_for_job(job.id)
        engineers = [a for a in agents if str(a.role) == "backend_engineer"]
        assert len(engineers) == 1, f"expected one engineer, got {len(engineers)} — CAS did not hold"
