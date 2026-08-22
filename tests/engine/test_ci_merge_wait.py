"""An approved PR must end up with a merge OWNER, never stranded.

Job 1ddb3283 is the receipt: reviewers approved 3 minutes after the PR opened,
CI was still running, the gate said mergeable_state=blocked, and run_task_review
marked the task MERGED anyway. The job walked to done and the approved PR sat
OPEN with nothing ever coming back for it — it merged only because a human was
watching.

The contract now: a state-level block gets a bounded wait (CI here settles in
seconds to minutes); past the wait the merge is handed to GitHub native
auto-merge, which completes server-side on green and stays visibly pending on
red. Only if even that handoff fails may the task advance with nothing owning
the merge — and that path must scream (auto_merge_stranded), because it is the
exact silent strand this file exists to prevent.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from minions.core.models import AgentRole, Task, TaskStatus


def _engine(db, wait_seconds=90):
    engine = MagicMock()
    engine.db = db
    engine.config = MagicMock()
    engine.config.model = "test-model"
    engine.config.job_cost_limit_usd = 1000.0
    engine.config.review_fanout_max = 0
    engine.config.model_reviewer = "test-reviewer"
    engine.config.require_ci_pass = True
    engine.config.ci_merge_wait_seconds = wait_seconds
    engine._k8s_enabled = False
    engine._nats_agent_status = AsyncMock()
    engine._trello_comment = AsyncMock()
    engine._maybe_dry_run = MagicMock(side_effect=lambda x: x)

    project = MagicMock()
    project.model = ""
    project.project_id = "flippin-balls/wallet-api"
    project.auto_merge = True
    project.git_provider = "gitlab"
    service = MagicMock(repo_path="/tmp", default_branch="main")
    engine._resolve_service.return_value = (project, service)
    return engine


async def _engineer_task(db, job):
    task = await db.create_task(
        Task(
            job_id=job.id,
            title="Add a thing",
            service="wallet-api",
            agent_role=AgentRole.BACKEND_ENGINEER,
            status=TaskStatus.PR_OPEN,
            branch_name="feat/x",
            pr_number=23,
            pr_url="https://github.com/flippin-balls/wallet-api/pull/23",
            mr_id="23",
        )
    )
    await db.update_task(task.id, status=TaskStatus.IN_REVIEW)
    return await db.get_task(task.id)


class _MergeProvider:
    """Engineer-App fake: merge state follows a script, calls are recorded."""

    def __init__(self, states, required=("lint",), enable_ok=True):
        self._states = list(states)
        self._required = list(required)
        self._enable_ok = enable_ok
        self.merged = False
        self.auto_merge_enabled = False
        self.state_reads = 0

    async def get_required_checks(self, project_id, branch):
        return self._required

    async def get_merge_state(self, project_id, mr_id):
        self.state_reads += 1
        if len(self._states) > 1:
            return self._states.pop(0)
        return self._states[0]

    async def merge_mr(self, project_id, mr_id):
        self.merged = True
        return {"merged": True}

    async def enable_auto_merge(self, project_id, mr_id):
        if self._enable_ok:
            self.auto_merge_enabled = True
            return {"enabled": True}
        return {"enabled": False, "error": "auto-merge not allowed on repo"}


def _approving_run():
    async def _run(**kwargs):
        result = kwargs["agent"]
        result.status = "done"
        result._review_verdict = "approve"
        return result

    return _run


async def _review(db, job, task, merge_provider, wait_seconds=90):
    engine = _engine(db, wait_seconds=wait_seconds)
    sleeps = []

    async def _instant_sleep(seconds):
        sleeps.append(seconds)

    with (
        patch("minions.engine.dev.run_agent", new=_approving_run()),
        patch(
            "minions.engine.review._create_provider_for_project",
            return_value=AsyncMock(get_changed_files=AsyncMock(return_value=["app/x.py"]), get_diff=AsyncMock(return_value="")),
        ),
        patch("minions.engine.review.create_engineer_provider", new=AsyncMock(return_value=merge_provider)),
        patch("asyncio.sleep", new=_instant_sleep),
    ):
        from minions.engine.dev import run_task_review

        await run_task_review(engine, job, task)
    return sleeps


async def _events(db, job_id, event_type):
    return [e for e in await db.get_events(job_id) if e["event_type"] == event_type]


class TestBoundedWait:
    async def test_a_block_that_clears_within_the_wait_merges_normally(self, db, sample_job):
        """The common case: CI finishes seconds after the panel does."""
        task = await _engineer_task(db, sample_job)
        provider = _MergeProvider(states=["blocked", "blocked", "clean"])

        sleeps = await _review(db, sample_job, task, provider)

        assert provider.merged, "the merge must happen once the state clears"
        assert not provider.auto_merge_enabled, "no deferral needed when the wait succeeded"
        assert len(sleeps) == 2, "one sleep per re-poll until the state cleared"
        assert (await db.get_task(task.id)).status == TaskStatus.MERGED

    async def test_a_config_level_block_does_not_wait(self, db, sample_job):
        """'No required checks' cannot change by re-polling — the wait would be
        pure delay on a repo that needs a human to gate it."""
        task = await _engineer_task(db, sample_job)
        provider = _MergeProvider(states=["clean"], required=())

        sleeps = await _review(db, sample_job, task, provider)

        assert sleeps == [], "config-level blocks must skip the poll loop"
        assert not provider.merged


class TestDeferralToGitHub:
    async def test_a_persistent_block_hands_the_merge_to_github(self, db, sample_job):
        task = await _engineer_task(db, sample_job)
        provider = _MergeProvider(states=["blocked"])

        sleeps = await _review(db, sample_job, task, provider, wait_seconds=90)

        assert provider.auto_merge_enabled, "past the wait, GitHub owns the merge"
        assert not provider.merged
        assert sum(sleeps) >= 90, "the full bounded wait must elapse first"
        assert len(await _events(db, sample_job.id, "auto_merge_deferred")) == 1
        assert await _events(db, sample_job.id, "auto_merge_stranded") == []
        # Advancing is now legitimate: the merge has an owner.
        assert (await db.get_task(task.id)).status == TaskStatus.MERGED

    async def test_a_failed_handoff_screams_stranded(self, db, sample_job):
        """The one remaining path where an open PR outlives its job must leave
        the loud, greppable trace — silence here is the original bug."""
        task = await _engineer_task(db, sample_job)
        provider = _MergeProvider(states=["blocked"], enable_ok=False)

        await _review(db, sample_job, task, provider)

        stranded = await _events(db, sample_job.id, "auto_merge_stranded")
        assert len(stranded) == 1
        assert "pull/23" in stranded[0]["detail"], "the event must name the PR a human now owns"
        assert await _events(db, sample_job.id, "auto_merge_deferred") == []
