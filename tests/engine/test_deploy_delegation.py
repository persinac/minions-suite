"""Merge is the pipeline's terminal responsibility; deployment is the repo's.

The old deploy leg could not have worked when asked to: report_deploy_status
injected the monitor's own task id, so the engineer tasks parked at DEPLOYING
were unmovable by construction, and check_deployed excluded the one task the
monitor could update. Every project ran deploy_target: "none" and every repo
deploys via its own CD — the leg was aspiration wearing a state machine.

These tests pin the replacement: MERGED passes straight through to DEPLOYED
regardless of deploy_target (a configured target is recorded as delegated,
never silently skipped, and never spawns an agent), and a job a previous
release left parked at DEPLOYING is healed rather than orphaned.
"""

from unittest.mock import MagicMock

from minions.core.models import AgentRole, JobStatus, Task, TaskStatus
from minions.engine.deploy import advance_merged_job, check_deployed
from tests.engine.test_dev import _mock_engine


async def _merged_job(db, engine, deploy_target="none"):
    job = await db.create_job("spec")
    for s in (
        JobStatus.SPEC_READY,
        JobStatus.TASKS_CREATED,
        JobStatus.DEV_IN_PROGRESS,
        JobStatus.PR_OPEN,
        JobStatus.REVIEW_IN_PROGRESS,
        JobStatus.MERGED,
    ):
        try:
            await db.update_job_status(job.id, s)
        except Exception:
            continue
    task = await db.create_task(Task(job_id=job.id, title="t", description="d", service="api", agent_role=AgentRole.BACKEND_ENGINEER))
    await db.update_task(task.id, status=TaskStatus.IN_PROGRESS)
    await db.update_task(task.id, pr_url="https://github.com/o/r/pull/1", pr_number=1, branch_name="feat/x")
    await db.update_task(task.id, status=TaskStatus.PR_OPEN)
    await db.update_task(task.id, status=TaskStatus.IN_REVIEW)
    await db.update_task(task.id, status=TaskStatus.MERGED, agent_role="code_reviewer")
    service = MagicMock(deploy_target=deploy_target, repo_path="/tmp", clone_url="")
    engine._resolve_service = MagicMock(return_value=(MagicMock(), service))
    return await db.get_job(job.id), await db.get_task(task.id)


async def _events(db, job_id, event_type):
    return [e for e in await db.get_events(job_id) if e.get("event_type") == event_type]


class TestMergedPassesThrough:
    async def test_no_target_advances_as_before(self, db):
        engine = _mock_engine(db)
        job, task = await _merged_job(db, engine, deploy_target="none")

        await advance_merged_job(engine, job)

        assert (await db.get_task(task.id)).status == TaskStatus.DONE
        assert (await db.get_job(job.id)).status == JobStatus.DEPLOYED

    async def test_a_real_target_advances_too_and_is_recorded_as_delegated(self, db):
        """The old code would have parked this job at DEPLOYING forever."""
        engine = _mock_engine(db)
        job, task = await _merged_job(db, engine, deploy_target="apprunner")

        await advance_merged_job(engine, job)

        assert (await db.get_task(task.id)).status == TaskStatus.DONE
        assert (await db.get_job(job.id)).status == JobStatus.DEPLOYED
        delegated = await _events(db, job.id, "deploy_delegated")
        assert delegated, "a configured target must be visibly delegated, not silently skipped"

    async def test_no_agent_and_no_monitor_task_are_created(self, db):
        engine = _mock_engine(db)
        job, _ = await _merged_job(db, engine, deploy_target="apprunner")

        await advance_merged_job(engine, job)

        tasks = await db.get_tasks(job.id)
        assert not [t for t in tasks if t.service == "_deploy"], "the monitor is gone"
        engine._run_in_process.assert_not_awaited()


class TestDeployingIsHealed:
    async def test_a_wedged_deploying_job_advances(self, db):
        """Jobs a pre-0.8.53 monitor left at DEPLOYING must not be orphaned."""
        engine = _mock_engine(db)
        job, task = await _merged_job(db, engine, deploy_target="apprunner")
        await db.update_task(task.id, status=TaskStatus.DEPLOYING)
        await db.update_job_status(job.id, JobStatus.DEPLOYING)

        await check_deployed(engine, await db.get_job(job.id))

        assert (await db.get_task(task.id)).status == TaskStatus.DONE
        assert (await db.get_job(job.id)).status == JobStatus.DEPLOYED
        assert await _events(db, job.id, "deploy_healed")

    async def test_the_engine_owns_terminal_bookkeeping(self, db):
        """DEPLOYED → DONE and _on_job_terminal belong to the engine's
        DEPLOYED branch — the old check_deployed also called _on_job_terminal,
        which double-fired the S3 upload and archive per job."""
        import inspect

        from minions.engine import deploy

        source = inspect.getsource(deploy)
        assert "_on_job_terminal" not in source.split('"""', 2)[2], "deploy handlers must not call _on_job_terminal"
