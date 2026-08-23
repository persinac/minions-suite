"""Reviewers must get a tree to read before the panel fans out.

External dispatch never creates /repos/<service> — the herder works in its
own clone — so the panel's file tools pointed at a directory that did not
exist. list_files answered [] as if the repo were empty rather than absent,
and job 7ba724fd's first panel launched exactly that blind (both reviewers
stopped without a verdict and needed the silent-reviewer nudge to recover).

The checkout is best effort: a review has always been able to proceed on
the diff alone, and still can — but these tests pin that a missing tree is
recorded rather than silent, and that no failure mode takes the panel down.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from minions.core.models import AgentRole, Task, TaskStatus

CLONE_URL = "https://github.com/flippin-balls/wallet-api.git"
REPO_PATH = "/repos/wallet-api"


def _engine(db):
    engine = MagicMock()
    engine.db = db
    engine.config = MagicMock()
    engine.config.model = "test-model"
    engine.config.job_cost_limit_usd = 1000.0
    engine.config.agent_cost_limit_usd = 100.0
    engine.config.require_ci_pass = False
    engine.config.review_fanout_max = 0
    engine.config.model_reviewer = "test-reviewer"
    engine.config.model_easy = "e"
    engine.config.model_medium = "m"
    engine.config.model_hard = "h"
    engine._k8s_enabled = False
    engine._nats_agent_status = AsyncMock()
    engine._trello_comment = AsyncMock()
    engine._maybe_dry_run = MagicMock(side_effect=lambda x: x)

    project = MagicMock()
    project.model = ""
    project.project_id = "flippin-balls/wallet-api"
    project.auto_merge = False
    project.git_provider = "gitlab"
    # Real strings, as projects.yaml provides them. The checkout helper
    # refuses non-string values by design.
    service = MagicMock(repo_path=REPO_PATH, clone_url=CLONE_URL, default_branch="main")
    engine._resolve_service.return_value = (project, service)
    return engine


async def _engineer_task(db, job, branch_name="feat/x"):
    task = await db.create_task(
        Task(
            job_id=job.id,
            title="Add a thing",
            service="wallet-api",
            agent_role=AgentRole.BACKEND_ENGINEER,
            status=TaskStatus.PR_OPEN,
            branch_name=branch_name,
            pr_number=23,
            pr_url="https://github.com/flippin-balls/wallet-api/pull/23",
            mr_id="23",
        )
    )
    await db.update_task(task.id, status=TaskStatus.IN_REVIEW)
    return await db.get_task(task.id)


def _approving_run_agent():
    async def _run(**kwargs):
        result = kwargs["agent"]
        result.status = "done"
        result._review_verdict = "approve"
        return result

    return _run


def _provider():
    provider = AsyncMock()
    provider.get_changed_files.return_value = ["app/service.py"]
    provider.get_diff.return_value = ""
    return provider


async def _run_review(db, job, task, checkout):
    engine = _engine(db)
    with (
        patch("minions.repos.ensure_checkout", new=checkout),
        patch("minions.engine.dev.run_agent", new=_approving_run_agent()),
        patch("minions.engine.review._create_provider_for_project", return_value=_provider()),
    ):
        from minions.engine.dev import run_task_review

        await run_task_review(engine, job, task)


def _panel_ran(tasks):
    return [t for t in tasks if t.agent_role == AgentRole.CODE_REVIEWER]


class TestCheckoutBeforeFanout:
    async def test_the_checkout_lands_on_the_pr_branch(self, db, sample_job):
        task = await _engineer_task(db, sample_job)
        checkout = AsyncMock(return_value=True)

        await _run_review(db, sample_job, task, checkout)

        checkout.assert_awaited_once_with(CLONE_URL, REPO_PATH, default_branch="feat/x")

    async def test_a_missing_tree_is_recorded_and_the_panel_still_runs(self, db, sample_job):
        task = await _engineer_task(db, sample_job)
        checkout = AsyncMock(return_value=False)

        await _run_review(db, sample_job, task, checkout)

        events = [e for e in await db.get_events(sample_job.id) if e.get("event_type") == "review_checkout_missing"]
        assert events, "a panel reviewing blind must leave a trace"
        assert _panel_ran(await db.get_tasks(sample_job.id)), "diff-only review must still happen"

    async def test_a_checkout_crash_does_not_take_the_panel_down(self, db, sample_job):
        task = await _engineer_task(db, sample_job)
        checkout = AsyncMock(side_effect=RuntimeError("network"))

        await _run_review(db, sample_job, task, checkout)

        events = [e for e in await db.get_events(sample_job.id) if e.get("event_type") == "review_checkout_missing"]
        assert events
        assert _panel_ran(await db.get_tasks(sample_job.id))

    async def test_no_branch_name_means_no_checkout(self, db, sample_job):
        """Moving an in-process engineer's tree to the default branch would be
        worse than leaving it — without the PR branch, do nothing."""
        task = await _engineer_task(db, sample_job, branch_name=None)
        checkout = AsyncMock(return_value=True)

        await _run_review(db, sample_job, task, checkout)

        checkout.assert_not_awaited()

    async def test_a_successful_checkout_leaves_no_missing_tree_event(self, db, sample_job):
        task = await _engineer_task(db, sample_job)
        checkout = AsyncMock(return_value=True)

        await _run_review(db, sample_job, task, checkout)

        events = [e for e in await db.get_events(sample_job.id) if e.get("event_type") == "review_checkout_missing"]
        assert not events
