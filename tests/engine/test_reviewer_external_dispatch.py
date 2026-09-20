"""The engine half of reviewer_dispatch="external": publish, then don't wedge.

Both reviewer launch paths used to create the agent row before doing anything
else, and an agent row is exactly what `find_claimable_work` reads as "somebody
already owns this". Publishing means the opposite: leave the task IN_PROGRESS
with NO agent, which is simultaneously what makes it claimable and what keeps
every recovery path off it — they all need an agent to reason about.

That second property is why the rescue tests below matter more than the publish
ones. A published item nobody claims is invisible to every existing check: a
job that looks healthy and never moves, which is the worst failure mode this
system has. Each path therefore has to grow its own bounded wait:

* the standalone review-type job — `check_review_tasks`, which the engine polls
  while the job is REVIEW_IN_PROGRESS
* the dev-job fan-out — `_await_external_review`, which waits inline because
  everything downstream (silent-reviewer re-run, discuss re-ask, aggregation,
  CI gate, auto-merge) is written against a dict of verdicts

and both have to handle the two opposite deaths: nobody claimed it, and
somebody claimed it and vanished.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from minions.core.models import Agent, AgentRole, JobStatus, Task, TaskStatus
from minions.engine import review as review_mod
from minions.engine.dev import _await_external_review

PR_URL = "https://github.com/flippin-balls/management-api/pull/84"


def _engine(db, *, reviewer_dispatch="external", claim_timeout=900, work_timeout=2700):
    engine = MagicMock()
    engine.db = db
    engine.config = MagicMock()
    engine.config.reviewer_dispatch = reviewer_dispatch
    engine.config.herder_claim_timeout_seconds = claim_timeout
    engine.config.herder_work_timeout_seconds = work_timeout
    engine.config.model = "test-model"
    engine.config.gitlab_url = ""
    engine._spawn = MagicMock()
    engine._nats_agent_status = AsyncMock()
    engine.registry = {}
    engine.memory_store = None
    return engine


async def _review_job(db, *, launched: bool = False) -> tuple[object, object]:
    """A review-type job with its single CODE_REVIEWER task.

    `launched=True` puts the job where the engine polls check_review_tasks from
    — REVIEW_IN_PROGRESS — which is what launch_review_tasks does before it
    publishes or spawns.
    """
    job, task = await db.create_review_job("fbf", PR_URL, "84")
    if launched:
        await db.update_job_status(job.id, JobStatus.REVIEW_IN_PROGRESS)
        job = await db.get_job(job.id)
    return job, task


async def _published_reviewer_task(db, job) -> object:
    """What the fan-out leaves behind: IN_PROGRESS, no agent row."""
    task = await db.create_task(
        Task(
            job_id=job.id,
            title="[api] Review PR",
            description="d",
            service="management-api",
            agent_role=AgentRole.CODE_REVIEWER,
            specialty="api",
            mr_url=PR_URL,
            mr_id="84",
            pr_url=PR_URL,
            pr_number=84,
        )
    )
    await db.update_task(task.id, status=TaskStatus.IN_PROGRESS)
    return await db.get_task(task.id)


async def _dev_job(db):
    job = await db.create_job("spec")
    for status in (JobStatus.SPEC_READY, JobStatus.TASKS_CREATED, JobStatus.DEV_IN_PROGRESS):
        await db.update_job_status(job.id, status)
    return await db.get_job(job.id)


# The unclaimed clock reads tasks.updated_at, and that column CANNOT be
# backdated: `trg_tasks_updated_at` is a BEFORE UPDATE trigger setting it to
# NOW(), so an UPDATE that tries to age a row silently produces a fresh
# timestamp instead. (An hour of "why does the fallback never fire" lives here.)
# So these tests shrink the budget rather than aging the row — same comparison,
# reached the same way, just with both sides small.
EXPIRED_BUDGET = 0.05


async def _let_the_claim_budget_lapse():
    await asyncio.sleep(EXPIRED_BUDGET * 3)


async def _age_agent(db, agent_id, seconds):
    stamp = (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()
    await db.update_agent(agent_id, started_at=stamp)


# =========================================================================
# The standalone review-type job (MR webhook / CLI)
# =========================================================================


class TestLaunchingAReviewJob:
    async def test_external_publishes_instead_of_launching(self, db):
        """No agent row is the mechanism, not a side effect — it is what
        find_claimable_work reads as unowned."""
        job, task = await _review_job(db)
        engine = _engine(db)

        await review_mod.launch_review_tasks(engine, job)

        assert engine._spawn.call_count == 0, "an in-process reviewer was launched anyway"
        assert await db.get_agent_for_task(task.id) is None, "the agent row makes the task unclaimable"

    async def test_the_task_is_still_claimed_as_in_progress(self, db):
        """Publishing is not "leave it pending". A PENDING task would be
        relaunched by the next poll, forever."""
        job, task = await _review_job(db)

        await review_mod.launch_review_tasks(_engine(db), job)

        assert (await db.get_task(task.id)).status == TaskStatus.IN_PROGRESS

    async def test_the_publish_is_recorded(self, db):
        """Otherwise "published and waiting" and "never launched" look identical
        in the audit trail."""
        job, task = await _review_job(db)

        await review_mod.launch_review_tasks(_engine(db), job)

        published = [e for e in await db.get_events(job.id) if e["event_type"] == "work_item_published"]
        assert len(published) == 1
        assert task.id in published[0]["detail"]
        assert "code_reviewer" in published[0]["detail"]

    async def test_in_process_still_launches(self, db):
        """The default. Merging this must change production behaviour not at all."""
        job, _ = await _review_job(db)
        engine = _engine(db, reviewer_dispatch="in_process")

        await review_mod.launch_review_tasks(engine, job)

        assert engine._spawn.call_count == 1

    async def test_in_process_records_no_publish(self, db):
        job, _ = await _review_job(db)

        await review_mod.launch_review_tasks(_engine(db, reviewer_dispatch="in_process"), job)

        assert [e for e in await db.get_events(job.id) if e["event_type"] == "work_item_published"] == []


class TestRescuingAnUnclaimedReviewJob:
    async def test_it_falls_back_in_process_once_the_claim_budget_is_spent(self, db):
        """Falling back costs API tokens; not falling back costs the job."""
        job, task = await _review_job(db, launched=True)
        await db.update_task(task.id, status=TaskStatus.IN_PROGRESS)
        engine = _engine(db, claim_timeout=EXPIRED_BUDGET)
        await _let_the_claim_budget_lapse()

        await review_mod.check_review_tasks(engine, await db.get_job(job.id))

        assert engine._spawn.call_count == 1
        assert "review-fallback" in engine._spawn.call_args.kwargs["name"]

    async def test_it_waits_before_falling_back(self, db):
        """A herder that is merely slow must not have its work duplicated by the
        metered path — that pays twice for one review."""
        job, task = await _review_job(db, launched=True)
        await db.update_task(task.id, status=TaskStatus.IN_PROGRESS)
        engine = _engine(db, claim_timeout=900)

        await review_mod.check_review_tasks(engine, await db.get_job(job.id))

        assert engine._spawn.call_count == 0

    async def test_the_timeout_is_recorded(self, db):
        job, task = await _review_job(db, launched=True)
        await db.update_task(task.id, status=TaskStatus.IN_PROGRESS)
        await _let_the_claim_budget_lapse()

        await review_mod.check_review_tasks(_engine(db, claim_timeout=EXPIRED_BUDGET), await db.get_job(job.id))

        timeouts = [e for e in await db.get_events(job.id) if e["event_type"] == "herder_claim_timeout"]
        assert len(timeouts) == 1
        assert "code_reviewer" in timeouts[0]["detail"]

    async def test_zero_disables_the_fallback(self, db):
        """Same semantics as the engineer path: 0 means an operator has decided
        the metered path must never run."""
        job, task = await _review_job(db, launched=True)
        await db.update_task(task.id, status=TaskStatus.IN_PROGRESS)
        engine = _engine(db, claim_timeout=0)
        await _let_the_claim_budget_lapse()

        await review_mod.check_review_tasks(engine, await db.get_job(job.id))

        assert engine._spawn.call_count == 0

    async def test_a_live_claim_is_left_alone(self, db):
        """A working herder must not have its review duplicated underneath it."""
        job, task = await _review_job(db, launched=True)
        await db.update_task(task.id, status=TaskStatus.IN_PROGRESS)
        await db.create_agent(Agent(job_id=job.id, role=AgentRole.CODE_REVIEWER, task_id=task.id, model="herder:a", status="running"))
        engine = _engine(db, claim_timeout=EXPIRED_BUDGET)
        await _let_the_claim_budget_lapse()

        await review_mod.check_review_tasks(engine, await db.get_job(job.id))

        assert engine._spawn.call_count == 0

    async def test_a_finished_in_process_run_is_not_re_run(self, db):
        """A done agent means the review already happened; the task is simply
        waiting on its own status write."""
        job, task = await _review_job(db, launched=True)
        await db.update_task(task.id, status=TaskStatus.IN_PROGRESS)
        await db.create_agent(Agent(job_id=job.id, role=AgentRole.CODE_REVIEWER, task_id=task.id, model="claude-sonnet-5", status="done"))
        engine = _engine(db, claim_timeout=EXPIRED_BUDGET)
        await _let_the_claim_budget_lapse()

        await review_mod.check_review_tasks(engine, await db.get_job(job.id))

        assert engine._spawn.call_count == 0

    async def test_in_process_dispatch_rescues_nothing(self, db):
        """The rescue must not fire when nothing was ever published. Under
        in_process an IN_PROGRESS task with no agent means a launch is in flight,
        and a second one would double-review the MR."""
        job, task = await _review_job(db, launched=True)
        await db.update_task(task.id, status=TaskStatus.IN_PROGRESS)
        engine = _engine(db, reviewer_dispatch="in_process", claim_timeout=EXPIRED_BUDGET)
        await _let_the_claim_budget_lapse()

        await review_mod.check_review_tasks(engine, await db.get_job(job.id))

        assert engine._spawn.call_count == 0


class TestRescuingAnAbandonedReviewClaim:
    """A killed pane or a closed laptop leaves the agent row reading "running"
    forever. That is neither unclaimed nor finished, so no other check acts."""

    async def test_a_stale_claim_is_released(self, db):
        job, task = await _review_job(db, launched=True)
        await db.update_task(task.id, status=TaskStatus.IN_PROGRESS)
        agent = await db.create_agent(Agent(job_id=job.id, role=AgentRole.CODE_REVIEWER, task_id=task.id, model="herder:gone", status="running"))
        await _age_agent(db, agent.id, 5000)

        await review_mod.check_review_tasks(_engine(db, work_timeout=2700), await db.get_job(job.id))

        assert (await db.get_agent(agent.id)).status == "failed"

    async def test_the_release_is_recorded(self, db):
        job, task = await _review_job(db, launched=True)
        await db.update_task(task.id, status=TaskStatus.IN_PROGRESS)
        agent = await db.create_agent(Agent(job_id=job.id, role=AgentRole.CODE_REVIEWER, task_id=task.id, model="herder:gone", status="running"))
        await _age_agent(db, agent.id, 5000)

        await review_mod.check_review_tasks(_engine(db, work_timeout=2700), await db.get_job(job.id))

        assert [e for e in await db.get_events(job.id) if e["event_type"] == "herder_claim_abandoned"]

    async def test_a_metered_in_process_reviewer_is_never_reaped_as_a_herder(self, db):
        """The `herder:` model prefix is the only thing distinguishing them, and
        reaping a live API-backed reviewer would abandon work already paid for."""
        job, task = await _review_job(db, launched=True)
        await db.update_task(task.id, status=TaskStatus.IN_PROGRESS)
        agent = await db.create_agent(Agent(job_id=job.id, role=AgentRole.CODE_REVIEWER, task_id=task.id, model="claude-sonnet-5", status="running"))
        await _age_agent(db, agent.id, 100000)

        await review_mod.check_review_tasks(_engine(db, work_timeout=2700), await db.get_job(job.id))

        assert (await db.get_agent(agent.id)).status == "running"

    async def test_a_young_claim_is_left_alone(self, db):
        job, task = await _review_job(db, launched=True)
        await db.update_task(task.id, status=TaskStatus.IN_PROGRESS)
        agent = await db.create_agent(Agent(job_id=job.id, role=AgentRole.CODE_REVIEWER, task_id=task.id, model="herder:busy", status="running"))
        await _age_agent(db, agent.id, 60)

        await review_mod.check_review_tasks(_engine(db, work_timeout=2700), await db.get_job(job.id))

        assert (await db.get_agent(agent.id)).status == "running"

    async def test_zero_disables_the_reaper(self, db):
        job, task = await _review_job(db, launched=True)
        await db.update_task(task.id, status=TaskStatus.IN_PROGRESS)
        agent = await db.create_agent(Agent(job_id=job.id, role=AgentRole.CODE_REVIEWER, task_id=task.id, model="herder:gone", status="running"))
        await _age_agent(db, agent.id, 100000)

        await review_mod.check_review_tasks(_engine(db, work_timeout=0), await db.get_job(job.id))

        assert (await db.get_agent(agent.id)).status == "running"


class TestTheReviewJobStillFinishes:
    async def test_a_herder_verdict_advances_the_job(self, db):
        """End to end for the standalone path: the task the herder closed is
        terminal, so check_review_tasks moves the job to DONE. Without the
        rescue pass leaving terminal tasks alone, this would be skipped."""
        job, task = await _review_job(db, launched=True)
        await db.update_task(task.id, status=TaskStatus.IN_PROGRESS)
        await db.update_task(task.id, status=TaskStatus.DONE, agent_role="", verdict="approve")

        await review_mod.check_review_tasks(_engine(db), await db.get_job(job.id))

        assert (await db.get_job(job.id)).status == JobStatus.DONE


# =========================================================================
# The dev-job fan-out
# =========================================================================


@pytest.fixture(autouse=True)
def fast_poll(monkeypatch):
    """The waiter sleeps a poll interval before its first read. Ten seconds per
    assertion is not a test suite."""
    monkeypatch.setattr("minions.engine.dev._EXTERNAL_REVIEW_POLL_SECONDS", 0.01)


class TestAwaitingAnExternalReviewer:
    async def test_a_verdict_comes_back(self, db):
        job = await _dev_job(db)
        task = await _published_reviewer_task(db, job)
        await db.update_task(task.id, status=TaskStatus.DONE, agent_role="", verdict="approve")

        handled, verdict = await _await_external_review(_engine(db), job, task, "api")

        assert handled is True
        assert verdict == "approve"

    async def test_an_unusable_verdict_reads_as_silence(self, db):
        """aggregate_verdicts fails closed on None, so garbage must not approve."""
        job = await _dev_job(db)
        task = await _published_reviewer_task(db, job)
        await db.update_task(task.id, status=TaskStatus.DONE, agent_role="", verdict="seems ok")

        handled, verdict = await _await_external_review(_engine(db), job, task, "api")

        assert handled is True
        assert verdict is None

    async def test_a_failed_reviewer_task_is_handled_not_re_run(self, db):
        """FAILED is a finished external attempt. Re-running it in-process here
        would pay for a review the fan-out is about to ask for again anyway."""
        job = await _dev_job(db)
        task = await _published_reviewer_task(db, job)
        await db.update_task(task.id, status=TaskStatus.FAILED, agent_role="", error="herder gave up")

        handled, verdict = await _await_external_review(_engine(db), job, task, "api")

        assert handled is True
        assert verdict is None

    async def test_it_publishes_before_waiting(self, db):
        job = await _dev_job(db)
        task = await _published_reviewer_task(db, job)
        await db.update_task(task.id, status=TaskStatus.DONE, agent_role="", verdict="approve")

        await _await_external_review(_engine(db), job, task, "api")

        published = [e for e in await db.get_events(job.id) if e["event_type"] == "work_item_published"]
        assert len(published) == 1
        assert "specialty=api" in published[0]["detail"]

    async def test_nobody_claims_it_so_it_falls_back(self, db):
        """The property that stops a published reviewer wedging the PR."""
        job = await _dev_job(db)
        task = await _published_reviewer_task(db, job)

        handled, verdict = await _await_external_review(_engine(db, claim_timeout=0.05), job, task, "api")

        assert handled is False, "the fan-out would have waited forever"
        assert verdict is None

    async def test_the_fallback_is_recorded(self, db):
        job = await _dev_job(db)
        task = await _published_reviewer_task(db, job)

        await _await_external_review(_engine(db, claim_timeout=0.05), job, task, "api")

        timeouts = [e for e in await db.get_events(job.id) if e["event_type"] == "herder_claim_timeout"]
        assert len(timeouts) == 1
        assert "code_reviewer" in timeouts[0]["detail"]

    async def test_a_claim_budget_of_zero_refuses_to_publish(self, db):
        """0 disables the fallback for engineers, where a later poll can still
        rescue the task. Here the waiter IS the only rescuer, so "disabled"
        would mean a coroutine spinning forever on a PR nobody is coming for."""
        job = await _dev_job(db)
        task = await _published_reviewer_task(db, job)

        handled, verdict = await _await_external_review(_engine(db, claim_timeout=0), job, task, "api")

        assert handled is False
        assert verdict is None
        assert [e for e in await db.get_events(job.id) if e["event_type"] == "work_item_published"] == []

    async def test_an_abandoned_claim_is_released_and_the_item_falls_back(self, db):
        """A killed pane holds the task forever otherwise: the row says
        "running", so the unclaimed clock never starts."""
        job = await _dev_job(db)
        task = await _published_reviewer_task(db, job)
        agent = await db.create_agent(Agent(job_id=job.id, role=AgentRole.CODE_REVIEWER, task_id=task.id, model="herder:gone", status="running"))
        await _age_agent(db, agent.id, 5000)

        handled, _ = await _await_external_review(_engine(db, claim_timeout=0.05, work_timeout=2700), job, task, "api")

        assert (await db.get_agent(agent.id)).status == "failed"
        assert handled is False
        assert [e for e in await db.get_events(job.id) if e["event_type"] == "herder_claim_abandoned"]

    async def test_a_working_herder_is_waited_for_past_the_claim_budget(self, db):
        """The claim clock must stop once somebody claims, or a herder that takes
        longer than herder_claim_timeout_seconds gets its work duplicated by the
        metered reviewer — the exact cost this feature exists to avoid."""
        job = await _dev_job(db)
        task = await _published_reviewer_task(db, job)
        await db.create_agent(Agent(job_id=job.id, role=AgentRole.CODE_REVIEWER, task_id=task.id, model="herder:busy", status="running"))
        engine = _engine(db, claim_timeout=0.02, work_timeout=2700)

        async def _finish_later():
            await asyncio.sleep(0.15)
            await db.update_task(task.id, status=TaskStatus.DONE, agent_role="", verdict="request_changes")

        finisher = asyncio.create_task(_finish_later())
        handled, verdict = await asyncio.wait_for(_await_external_review(engine, job, task, "api"), timeout=10)
        await finisher

        assert handled is True
        assert verdict == "request_changes"

    async def test_a_vanished_task_is_not_re_run(self, db, monkeypatch):
        """Cancelled underneath us. Running it in-process would resurrect work
        somebody deliberately removed — and the waiter must not spin on a row
        that will never reappear."""
        job = await _dev_job(db)
        task = await _published_reviewer_task(db, job)
        monkeypatch.setattr(db, "get_task", AsyncMock(return_value=None))

        handled, verdict = await asyncio.wait_for(_await_external_review(_engine(db, claim_timeout=0.05), job, task, "api"), timeout=10)

        assert handled is True, "a task that no longer exists would have been re-run in-process"
        assert verdict is None


class TestTheFanOutUsesTheWaiter:
    async def test_external_dispatch_waits_instead_of_creating_an_agent(self, db):
        """The ordering IS the mechanism: an agent row created first is what made
        reviewer tasks unclaimable in the first place."""
        from minions.engine import dev as dev_mod

        job = await _dev_job(db)
        engine = _engine(db)
        seen = {}

        async def _fake_wait(engine_, job_, reviewer_task, specialty):
            seen["agent"] = await db.get_agent_for_task(reviewer_task.id)
            seen["task_id"] = reviewer_task.id
            return True, "approve"

        parent = await db.create_task(
            Task(job_id=job.id, title="t", description="d", service="management-api", agent_role=AgentRole.BACKEND_ENGINEER, pr_url=PR_URL)
        )
        with patch.object(dev_mod, "_await_external_review", new=_fake_wait):
            specialty, verdict = await dev_mod._run_one_specialist(engine, job, parent, "api", None, None, "84", {}, None, "ctx")

        assert (specialty, verdict) == ("api", "approve")
        assert seen["agent"] is None, "an agent row existed before the item was published"
        assert await db.get_agent_for_task(seen["task_id"]) is None, "an in-process reviewer ran anyway"

    async def test_a_refused_wait_falls_through_to_the_in_process_reviewer(self, db):
        """(False, None) means nobody came. The metered reviewer must then run —
        otherwise the PR waits on a verdict that will never arrive."""
        from minions.engine import dev as dev_mod

        job = await _dev_job(db)
        engine = _engine(db)
        engine.config.review_fanout_max = 0
        engine._k8s_enabled = False

        async def _refuse(*_a, **_k):
            return False, None

        async def _approving_run_agent(**kwargs):
            result = kwargs["agent"]
            result.status = "done"
            result._review_verdict = "approve"
            return result

        parent = await db.create_task(
            Task(job_id=job.id, title="t", description="d", service="management-api", agent_role=AgentRole.BACKEND_ENGINEER, pr_url=PR_URL)
        )
        with (
            patch.object(dev_mod, "_await_external_review", new=_refuse),
            patch.object(dev_mod, "run_agent", new=_approving_run_agent),
            patch.object(dev_mod, "resolve_model", return_value="claude-sonnet-5"),
        ):
            specialty, verdict = await dev_mod._run_one_specialist(engine, job, parent, "api", None, None, "84", {}, None, "ctx")

        assert (specialty, verdict) == ("api", "approve")
        reviewer_tasks = [t for t in await db.get_tasks(job.id) if t.agent_role == AgentRole.CODE_REVIEWER]
        assert len(reviewer_tasks) == 1
        agent = await db.get_agent_for_task(reviewer_tasks[0].id)
        assert agent is not None, "the in-process fallback never ran"
        assert agent.model == "claude-sonnet-5"

    async def test_in_process_dispatch_never_consults_the_waiter(self, db):
        """The default path must not even reach the new code."""
        from minions.engine import dev as dev_mod

        job = await _dev_job(db)
        engine = _engine(db, reviewer_dispatch="in_process")
        engine._k8s_enabled = False
        called = {"n": 0}

        async def _tripwire(*_a, **_k):
            called["n"] += 1
            return True, None

        async def _approving_run_agent(**kwargs):
            result = kwargs["agent"]
            result.status = "done"
            result._review_verdict = "approve"
            return result

        parent = await db.create_task(
            Task(job_id=job.id, title="t", description="d", service="management-api", agent_role=AgentRole.BACKEND_ENGINEER, pr_url=PR_URL)
        )
        with (
            patch.object(dev_mod, "_await_external_review", new=_tripwire),
            patch.object(dev_mod, "run_agent", new=_approving_run_agent),
            patch.object(dev_mod, "resolve_model", return_value="claude-sonnet-5"),
        ):
            await dev_mod._run_one_specialist(engine, job, parent, "api", None, None, "84", {}, None, "ctx")

        assert called["n"] == 0
