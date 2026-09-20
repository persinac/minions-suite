"""A review panel interrupted by a restart must be resumable, not wedged.

`run_task_review` is a fire-and-forget coroutine (`engine._spawn`, dev.py) that
holds the whole round in memory as a `verdicts` dict. The shutdown drain waits
for in-flight AGENTS, not engine bookkeeping, so `stop()` cancels it outright —
and a crash, eviction or OOM does the same without even the courtesy of a drain.

What it leaves behind was handled by NOTHING before this module existed:

* `manage_dev_tasks` walks only engineer-role tasks (dev.py), so the
  CODE_REVIEWER child rows the fan-out creates are never examined on any tick.
* the parent's IN_REVIEW branch reads `get_agent_for_task(parent)`, which
  returns the ENGINEER agent (status done) — neither the "starting" nor the
  "failed" recovery fires.
* re-entry is refused anyway: the fan-out guard returns whenever non-FAILED
  reviewer rows exist for the same (pr_url, revision_count).

So the parent sat IN_REVIEW forever, every tick, silently.

Under `reviewer_dispatch="external"` it is strictly worse. The child sits
IN_PROGRESS with NO agent row, so `_startup_cleanup` — which iterates agents —
never sees it, while `find_claimable_work` is role-agnostic and still offers it.
A herder claims it and writes a real verdict onto the row. The coroutine that
was going to read that verdict is gone. The verdict is paid for and discarded.

The fix is recovery, not write-ordering: ordering cannot survive a process that
stops between any two statements. These tests follow the house pattern for that
(see test_review_transition_recovery.py) — fabricate the intermediate database
state and call the entry point, rather than trying to simulate a crash.
"""

import asyncio
from unittest.mock import patch

from minions.core.models import AgentRole, JobStatus, Task, TaskStatus
from minions.engine.dev import manage_dev_tasks
from tests.engine.test_dev import _mock_engine
from tests.engine.test_review_fanout import _engine, _provider

PR = "https://github.com/flippin-balls/wallet-api/pull/23"


async def _dev_job(db):
    job = await db.create_job("spec")
    for status in (JobStatus.SPEC_READY, JobStatus.TASKS_CREATED, JobStatus.DEV_IN_PROGRESS):
        await db.update_job_status(job.id, status)
    return job


async def _parent_in_review(db, job, revision_count=0):
    """An engineer task parked at IN_REVIEW with a PR, as the fan-out finds it."""
    task = await db.create_task(
        Task(job_id=job.id, title="Add a thing", description="d", service="wallet-api", agent_role=AgentRole.BACKEND_ENGINEER)
    )
    await db.update_task(task.id, status=TaskStatus.IN_PROGRESS)
    await db.update_task(task.id, pr_url=PR, pr_number=23, mr_id="23", branch_name="feat/x", revision_count=revision_count)
    await db.update_task(task.id, status=TaskStatus.PR_OPEN)
    await db.update_task(task.id, status=TaskStatus.IN_REVIEW)
    return await db.get_task(task.id)


async def _reviewer_child(db, job, parent, specialty, verdict=None, status=TaskStatus.DONE, revision_count=None):
    """Exactly what `_run_one_specialist` leaves behind, at a chosen stage.

    `status=IN_PROGRESS` with no verdict is the published-but-unfinished shape:
    under external dispatch it also has no agent row, which is what makes it
    invisible to startup cleanup.
    """
    round_number = parent.revision_count if revision_count is None else revision_count
    child = await db.create_task(
        Task(
            job_id=job.id,
            title=f"[{specialty}] Review PR for {parent.title}",
            description="d",
            service=parent.service,
            agent_role=AgentRole.CODE_REVIEWER,
            status=TaskStatus.IN_PROGRESS,
            specialty=specialty,
            revision_count=round_number,
            pr_url=PR,
            pr_number=23,
            mr_id="23",
            mr_url=PR,
        )
    )
    if status != TaskStatus.IN_PROGRESS:
        # `_run_one_specialist` writes `verdict or ""`, so a reviewer that
        # returned nothing usable leaves an EMPTY STRING, not NULL.
        await db.update_task(child.id, status=status, agent_role="", verdict=verdict or "")
    return await db.get_task(child.id)


def _spawned_names(engine):
    return [call.kwargs.get("name", "") for call in engine._spawn.call_args_list]


async def _events(db, job, event_type):
    return [e for e in await db.get_events(job.id) if e["event_type"] == event_type]


class TestTheOrphanedPanelIsNoticed:
    """The anchor. On the unfixed engine nothing here fires at all."""

    async def test_an_orphaned_panel_is_resumed(self, db):
        job = await _dev_job(db)
        parent = await _parent_in_review(db, job)
        await _reviewer_child(db, job, parent, "api", "approve")
        await _reviewer_child(db, job, parent, "backend-architecture", "approve")
        engine = _mock_engine(db)

        await manage_dev_tasks(engine, await db.get_job(job.id))

        assert any(n == f"review-{parent.id[:8]}" for n in _spawned_names(engine)), (
            f"a panel with no owning coroutine must be re-entered; spawned={_spawned_names(engine)}"
        )

    async def test_a_parent_with_no_panel_at_all_is_left_alone(self, db):
        """IN_REVIEW with no reviewer rows is a different state — not ours."""
        job = await _dev_job(db)
        parent = await _parent_in_review(db, job)
        engine = _mock_engine(db)

        await manage_dev_tasks(engine, await db.get_job(job.id))

        assert not any(n == f"review-{parent.id[:8]}" for n in _spawned_names(engine))

    async def test_a_live_panel_is_not_resumed(self, db):
        """Anti-thrash: the poll runs every few seconds; a running panel owns the task.

        Without this the reconciler re-spawns a second panel on every tick while
        the first is still collecting verdicts — duplicate reviewers, which is
        the $4.87 failure the fan-out guard was built for in the first place.
        """
        job = await _dev_job(db)
        parent = await _parent_in_review(db, job)
        await _reviewer_child(db, job, parent, "api", status=TaskStatus.IN_PROGRESS)
        engine = _mock_engine(db)

        async def _still_running():
            await asyncio.sleep(3600)

        live = asyncio.create_task(_still_running(), name=f"review-{parent.id[:8]}")
        engine._background_tasks = {live}
        try:
            await manage_dev_tasks(engine, await db.get_job(job.id))
        finally:
            live.cancel()

        assert not any(n == f"review-{parent.id[:8]}" for n in _spawned_names(engine)), "a panel already in flight must not be re-spawned"


class TestResumingRebuildsTheRound:
    """`run_task_review` re-entered on an existing panel must finish it from rows."""

    async def _resume(self, db, job, parent, run=None, changed_files=("app/api/service.py",)):
        """Re-enter `run_task_review` on an existing panel, counting reviewer runs.

        The spy COUNTS rather than raising. `_collect_verdicts` gathers with
        `return_exceptions=True`, so an assert inside a fake `run_agent` is
        swallowed and silently becomes a None verdict — a guard written that
        way cannot fail the test it was added to protect.
        """
        engine = _engine(db)
        calls: list[str] = []

        async def _counting(**kwargs):
            calls.append(kwargs["task"].specialty)
            result = kwargs["agent"]
            result.status = "done"
            result._review_verdict = "approve"
            return result

        if run is None:
            run = _counting
        with (
            patch("minions.engine.dev.run_agent", new=run),
            patch("minions.engine.review._create_provider_for_project", return_value=_provider(list(changed_files))),
            patch("minions.repos.ensure_checkout", return_value=True),
        ):
            from minions.engine.dev import run_task_review

            await run_task_review(engine, job, parent)
        return calls

    async def test_the_resume_is_recorded(self, db):
        """A silent self-heal hides how often a rollout lands mid-review.

        Recorded inside `run_task_review` rather than by the reconciler, so it
        marks a panel that was actually resumed and covers every entry path,
        not just the poll-loop one.
        """
        job = await _dev_job(db)
        parent = await _parent_in_review(db, job)
        await _reviewer_child(db, job, parent, "api", "approve")

        await self._resume(db, job, parent)

        assert await _events(db, job, "review_panel_resumed"), "a resume must be visible afterwards, not merely quiet"

    async def test_a_finished_panel_aggregates_without_rerunning_anyone(self, db):
        job = await _dev_job(db)
        parent = await _parent_in_review(db, job)
        await _reviewer_child(db, job, parent, "api", "approve")
        await _reviewer_child(db, job, parent, "backend-architecture", "approve")

        calls = await self._resume(db, job, parent)

        assert calls == [], f"a resumed round must run nobody: {calls}"
        assert (await db.get_task(parent.id)).status == TaskStatus.MERGED
        aggregated = await _events(db, job, "review_aggregated")
        assert aggregated, "a resumed round must still record its aggregate"

    async def test_an_objection_on_the_rebuilt_round_still_blocks(self, db):
        """Fail-closed survives the rebuild — the whole point of reading rows."""
        job = await _dev_job(db)
        parent = await _parent_in_review(db, job)
        await _reviewer_child(db, job, parent, "api", "request_changes")
        await _reviewer_child(db, job, parent, "backend-architecture", "approve")

        await self._resume(db, job, parent)

        refreshed = await db.get_task(parent.id)
        assert refreshed.status == TaskStatus.IN_PROGRESS, "a rebuilt objection must route to revision"
        assert (refreshed.review_status or "").startswith("changes_requested")

    async def test_carried_approvals_are_named_in_the_resumed_aggregate(self, db):
        """Carried approvals have NO row at the current revision.

        Reading only current-round rows does not flip the verdict — carried
        entries are all APPROVE and `aggregate_verdicts` ignores absent keys
        rather than failing closed on them. What it does is under-report the
        panel: the record then claims one specialist approved this PR when two
        did, and the specialist that was deliberately skipped to save a re-run
        looks like it never saw the code.
        """
        job = await _dev_job(db)
        parent = await _parent_in_review(db, job, revision_count=1)
        await _reviewer_child(db, job, parent, "api", "approve", revision_count=0)
        await _reviewer_child(db, job, parent, "backend-architecture", "approve", revision_count=1)

        await self._resume(db, job, parent)

        reason = (await _events(db, job, "review_aggregated"))[0]["detail"]
        assert "api" in reason, f"the carried approval must survive the rebuild: {reason}"
        assert "backend-architecture" in reason, reason

    async def test_a_row_with_no_usable_verdict_is_still_chased(self, db):
        """`_run_one_specialist` writes `verdict or ""`, so a crashed reviewer leaves ''.

        The property that matters is that the row still APPEARS in the rebuilt
        dict. Drop it and the specialty is simply absent — `aggregate_verdicts`
        ignores absent keys rather than failing closed on them, so the panel
        silently shrinks and a reviewer that never answered looks like one that
        was never needed.

        Note the mapping of '' to None in `_reconstruct_verdicts` is defensive,
        not load-bearing: `normalise_verdict('')` is already None, so every
        consumer treats the two alike. Verified rather than assumed — a
        mutation that removed the mapping left this suite green.
        """
        job = await _dev_job(db)
        parent = await _parent_in_review(db, job)
        await _reviewer_child(db, job, parent, "api", "")
        await _reviewer_child(db, job, parent, "backend-architecture", "approve")

        reran: list[str] = []

        async def _run(**kwargs):
            reran.append(kwargs["task"].specialty)
            result = kwargs["agent"]
            result.status = "done"
            result._review_verdict = "approve"
            return result

        await self._resume(db, job, parent, run=_run)

        assert reran == ["api"], f"the empty verdict must be chased exactly once: {reran}"

    async def test_a_verdict_written_after_the_restart_is_honoured(self, db):
        """The external case: a herder finished the work the dead coroutine published.

        The child was IN_PROGRESS with no agent row when the engine died, so
        startup cleanup never saw it, but a herder claimed it and wrote a real
        verdict. Before this, that verdict was paid for and discarded.
        """
        job = await _dev_job(db)
        parent = await _parent_in_review(db, job)
        child = await _reviewer_child(db, job, parent, "api", status=TaskStatus.IN_PROGRESS)
        await db.update_task(child.id, status=TaskStatus.DONE, agent_role="", verdict="request_changes")

        await self._resume(db, job, parent)

        refreshed = await db.get_task(parent.id)
        assert refreshed.status == TaskStatus.IN_PROGRESS
        assert "api" in (refreshed.review_status or ""), refreshed.review_status


class TestOneShotStepsAreNotRespent:
    async def test_a_spent_silent_rerun_is_not_run_again(self, db):
        """The re-run is once per round, and a restart must not reset that.

        `review_silent_rerun` is recorded BEFORE the work, so its presence means
        "already started", not "already finished". Reading it as spent is the
        conservative choice: never pay twice, and anything still missing is
        caught by `aggregate_verdicts` failing closed.
        """
        job = await _dev_job(db)
        parent = await _parent_in_review(db, job)
        await _reviewer_child(db, job, parent, "api", "")
        await _reviewer_child(db, job, parent, "backend-architecture", "approve")
        # The real detail format, including `revision=`. Without it the marker
        # does not match and this test passes for the wrong reason.
        await db.record_event(job.id, "review_silent_rerun", "engine", f"task={parent.id} revision=0 rerun=api")

        engine = _engine(db)
        calls: list[str] = []

        async def _counting(**kwargs):
            # Counts; does not raise. `_collect_verdicts` gathers with
            # return_exceptions=True, so a raising guard here is swallowed and
            # the test can never fail.
            calls.append(kwargs["task"].specialty)
            result = kwargs["agent"]
            result.status = "done"
            result._review_verdict = "approve"
            return result

        with (
            patch("minions.engine.dev.run_agent", new=_counting),
            patch("minions.engine.review._create_provider_for_project", return_value=_provider(["app/api/service.py"])),
            patch("minions.repos.ensure_checkout", return_value=True),
        ):
            from minions.engine.dev import run_task_review

            await run_task_review(engine, job, parent)

        assert calls == [], f"the re-run was already spent before the resume; it must not be bought again: {calls}"
        refreshed = await db.get_task(parent.id)
        assert refreshed.status == TaskStatus.IN_PROGRESS, "an unusable verdict must still fail closed"
