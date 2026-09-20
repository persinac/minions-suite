"""At most one LIVE agent per task, enforced by the database.

`find_claimable_work` decides ownership in Python — "does this task have an agent
in starting/running?" — and the caller then creates the row. Between those two
steps a second worker runs the same check, sees the same answer, and both win.
The duplicate herder spawn is that race, and no tighter check inside that
function can close it, because the race is in the database.

So the database closes it, with a partial unique index. The shape matters more
than it looks, and the two halves of this file are the two halves of the shape:

* `TestTheRaceIsClosed` — a second LIVE agent on one task is refused, and refused
  as a typed `AgentClaimConflictError` rather than a psycopg exception.
* `TestTheNormalPathStillWorks` — everything else still inserts. A task
  legitimately accumulates SEVERAL agent rows over its life (first attempt, every
  retry, every revision round), and they all settle into the SAME terminal
  status. The note this work came from prescribed a unique index on
  `(task_id, status)`; that would have rejected the second revision's completion.
  These tests are the ones a pair constraint fails.

`TestTheConstraintIsActuallyThere` is the vacuity guard. Tests build their schema
from `tests/conftest_pg_schema.sql`, NOT from `database/pgsql/migrations/`, so a
migration-only change leaves every test above green while enforcing nothing.
"""

import asyncio

import pytest

from minions.core.models import Agent, AgentRole, Task, TaskStatus
from minions.db import AgentClaimConflictError

LIVE_STATUSES = ("starting", "running")


async def _task(db, title: str = "Sanitize CSV cells") -> Task:
    job = await db.create_job("Neutralize CSV formula injection in report exports")
    task = await db.create_task(
        Task(
            job_id=job.id,
            title=title,
            description="Prefix dangerous cells with an apostrophe",
            service="management-api",
            agent_role=AgentRole.BACKEND_ENGINEER,
        )
    )
    return task


def _agent(task: Task | None, status: str = "running", model: str = "claude-sonnet-5", job_id: str | None = None) -> Agent:
    """An agent row. `task=None` is the taskless case, which still needs a job.

    `agents.job_id` is NOT NULL — a spec analyst belongs to a job even before
    there are any tasks to belong to — so the taskless case passes `job_id`
    directly rather than inheriting it from a task.
    """
    task_id = None
    if task is not None:
        job_id = task.job_id
        task_id = task.id
    return Agent(job_id=job_id, task_id=task_id, role=AgentRole.BACKEND_ENGINEER, model=model, status=status)


class TestTheRaceIsClosed:
    @pytest.mark.parametrize("first", LIVE_STATUSES)
    @pytest.mark.parametrize("second", LIVE_STATUSES)
    async def test_a_second_live_agent_on_one_task_is_refused(self, db, first, second):
        """Both live statuses, in every pairing — the index covers the set, not one value.

        `starting` and `running` are both live: an agent is created `starting`
        and only becomes `running` once its loop begins, so a claim that lands
        in the gap must still lose.
        """
        task = await _task(db)
        await db.create_agent(_agent(task, status=first))

        with pytest.raises(AgentClaimConflictError):
            await db.create_agent(_agent(task, status=second))

    async def test_the_conflict_names_the_task_and_is_not_a_driver_error(self, db):
        """Typed at the db layer so nothing above it has to import psycopg.

        A leaked `UniqueViolation` in the MCP server or the engine would make
        `AbstractDatabase` a description of one implementation rather than a
        protocol, and callers would be matching on a driver's class to decide
        an ordinary orchestration outcome.
        """
        task = await _task(db)
        await db.create_agent(_agent(task))

        with pytest.raises(AgentClaimConflictError) as caught:
            await db.create_agent(_agent(task))

        assert caught.value.task_id == task.id
        assert type(caught.value).__module__.startswith("minions.")

    async def test_only_one_of_many_simultaneous_claims_survives(self, db):
        """The property under real concurrency, not a simulated interleaving.

        Eight coroutines insert at once against a shared pool. Whether they
        serialize or genuinely collide is up to the scheduler, so this asserts
        the invariant that holds either way: exactly one row is live. Without
        the index the same test lands eight.
        """
        task = await _task(db)

        results = await asyncio.gather(
            *(db.create_agent(_agent(task)) for _ in range(8)),
            return_exceptions=True,
        )

        winners = [r for r in results if isinstance(r, Agent)]
        losers = [r for r in results if isinstance(r, AgentClaimConflictError)]
        assert len(winners) == 1
        assert len(losers) == 7, f"unexpected failures: {[r for r in results if not isinstance(r, Agent | AgentClaimConflictError)]}"
        assert await _live_count(db, task.id) == 1


class TestTheNormalPathStillWorks:
    async def test_a_retry_may_claim_once_the_previous_attempt_is_terminal(self, db):
        """The recovery contract: every path marks the old row terminal BEFORE relaunching.

        Startup recovery, the abandoned-claim sweep, `release_engineer_work` and
        `complete_engineer_work` all do this already. If one ever stopped, this
        test stays green and the retry starts failing instead — which is the
        point: the index turns a silent double-run into a loud refusal.
        """
        task = await _task(db)
        first = await db.create_agent(_agent(task))

        await db.update_agent(first.id, status="failed", error="orphaned by restart")
        second = await db.create_agent(_agent(task))

        assert second.id != first.id
        assert await _live_count(db, task.id) == 1

    async def test_several_agents_for_one_task_may_share_a_terminal_status(self, db):
        """The regression a `(task_id, status)` pair constraint would have caused.

        `run_engineer` creates a fresh agent row for the first attempt, for every
        retry, and for every revision round of the SAME task, and they all settle
        into `completed`. A pair constraint would reject the second revision's
        completion — breaking the normal path in order to close a race on the
        unusual one.
        """
        task = await _task(db)

        for _ in range(3):
            agent = await db.create_agent(_agent(task))
            await db.update_agent(agent.id, status="completed")

        agents = await db.get_agents_for_job(task.job_id)
        assert len(agents) == 3
        assert {a.status for a in agents} == {"completed"}
        assert await _live_count(db, task.id) == 0

    async def test_live_agents_on_different_tasks_do_not_collide(self, db):
        """Sibling tasks of one job run in parallel by design."""
        first = await _task(db, title="Sanitize CSV cells")
        second = await _task(db, title="Escape spreadsheet formulas")

        await db.create_agent(_agent(first))
        await db.create_agent(_agent(second))

        assert await _live_count(db, first.id) == 1
        assert await _live_count(db, second.id) == 1

    async def test_agents_with_no_task_are_unconstrained(self, db):
        """The spec analyst and the arbiter carry no task_id.

        Postgres treats NULLs as distinct in a unique index, so they fall
        outside the constraint for free — but only by accident of SQL
        semantics, which is worth pinning down.
        """
        job = await db.create_job("Neutralize CSV formula injection in report exports")

        await db.create_agent(_agent(None, model="spec-analyst", job_id=job.id))
        await db.create_agent(_agent(None, model="arbiter", job_id=job.id))

        assert await _live_count(db, None) == 2


class TestTheConstraintIsActuallyThere:
    async def test_the_index_exists_in_the_schema_these_tests_run_against(self, db):
        """Vacuity guard.

        The suite builds `minions_test` from `tests/conftest_pg_schema.sql`;
        dbmate applies `database/pgsql/migrations/`. Nothing reconciles the two,
        and `tests/test_deployed_schema_gate.py` compares migrations to the
        DEPLOYED database rather than to that file. So a migration added without
        the matching line here enforces nothing, every test above passes by
        inserting freely, and the gap is invisible until production.
        """
        import minions.db.postgres as pg_mod

        async with db._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT indexdef FROM pg_indexes WHERE schemaname = %s AND indexname = %s",
                (pg_mod.JOB_SCHEMA, pg_mod.LIVE_AGENT_INDEX),
            )
            row = await cur.fetchone()

        assert row is not None, f"{pg_mod.LIVE_AGENT_INDEX} is missing from tests/conftest_pg_schema.sql — every test in this file is vacuous"
        indexdef = row["indexdef"]
        assert "UNIQUE" in indexdef
        for status in LIVE_STATUSES:
            assert status in indexdef, f"index no longer covers {status!r}: {indexdef}"


async def _live_count(db, task_id: str | None) -> int:
    """Live agent rows for a task, counted in SQL rather than through the model.

    The whole claim is about what the DATABASE permits, so reading it back
    through `get_agents_for_job` would only re-assert what the inserts already
    returned.
    """
    import minions.db.postgres as pg_mod

    predicate = "task_id = %s"
    params: tuple = (task_id,)
    if task_id is None:
        predicate = "task_id IS NULL"
        params = ()

    async with db._pool.connection() as conn:
        cur = await conn.execute(
            f"SELECT COUNT(*) AS n FROM {pg_mod.JOB_SCHEMA}.agents WHERE {predicate} AND status = ANY(%s)",
            (*params, list(LIVE_STATUSES)),
        )
        row = await cur.fetchone()
        return row["n"]


class TestTaskStatusIsIrrelevantToTheIndex:
    async def test_a_completed_task_still_refuses_a_second_live_agent(self, db):
        """The constraint keys on the task's IDENTITY, not on its status.

        Worth pinning because the Python check it backs up reads the task list,
        so it is tempting to assume task status filters the constraint too. It
        does not, and should not: a task that finished while a claim was in
        flight still must not acquire a second worker.
        """
        task = await _task(db)
        await db.update_task(task.id, status=TaskStatus.IN_PROGRESS)
        await db.create_agent(_agent(task))
        # `agent_role=""` because `in_progress -> done` is restricted to the
        # reviewer, database engineer and deploy monitor. The engine itself
        # clears the role for exactly this reason (`review.py`), and which role
        # closes the task is not what this test is about.
        await db.update_task(task.id, status=TaskStatus.DONE, agent_role="")

        with pytest.raises(AgentClaimConflictError):
            await db.create_agent(_agent(task))
