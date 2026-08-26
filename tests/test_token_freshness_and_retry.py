"""Regressions from job 03836165, which died on an expired GitHub App token.

Timeline, from the engine log:

    01:37:27  token minted, "expires in 3599s"  -> dies 02:37:26
    02:32:26  refresh due (300s margin) -- DID NOT HAPPEN
    02:47:05  git clone -> "Invalid username or token"
    02:47:22  "Auto-retry: task ... reset to pending (attempt 2/3)"
    02:47:22  "Job 03836165 -> failed"          <- same second
    02:47:30  token minted                       <- 25s too late

Three distinct defects, each of which alone would have been survivable:

1. GH_TOKEN was refreshed from exactly one place -- the top of the engine poll
   loop -- so its freshness depended on an unrelated loop's health, and the
   paths that actually spend it (clone, push, gh pr create) trusted ambient
   state. An expired credential is silent until something uses it.

2. The auto-retry reset the task to PENDING in the database, then the terminal
   check read a STALE in-memory snapshot that still said FAILED, and failed the
   whole job -- cancelling the retry it had just scheduled.

3. Unrelated but surfaced by the same run: a completed agent stops
   heartbeating, so it gets reported stale and kill-signalled.
"""

from pathlib import Path

from minions.core.models import TaskStatus


class TestTokenRefreshedAtPointOfSpend:
    """Every git operation that authenticates must mint immediately before it.

    Source inspection: these call subprocesses and a real provider, so the
    honest unit-level assertion is that the refresh is present in the path.
    """

    def _source(self, module) -> str:
        return Path(module.__file__).read_text(encoding="utf-8")

    def _body(self, module, func_name: str) -> str:
        src = self._source(module)
        start = src.index(f"def {func_name}")
        nxt = src.find("\n    async def ", start + 1)
        if nxt == -1:
            nxt = src.find("\n    def ", start + 1)
        if nxt == -1:
            nxt = len(src)
        return src[start:nxt]

    def test_clone_refreshes_before_git_runs(self):
        """The failure that killed the job: clone ran on an hour-old token."""
        from minions import repos

        body = self._body(repos, "ensure_checkout")

        assert "refresh_env_token" in body, "ensure_checkout clones without refreshing the token"
        assert body.index("refresh_env_token") < body.index('_run_git("fetch"'), "token must be refreshed BEFORE the first network git call"

    def test_push_refreshes_before_git_runs(self):
        """The worst case: an agent works 40 minutes, then pushes on a token
        minted before it started. All the work is already done."""
        from minions.agents.tools import mcp_executor

        body = self._body(mcp_executor, "_push")

        assert "refresh_env_token" in body, "_push runs git push without refreshing the token"
        assert body.index("refresh_env_token") < body.index("create_subprocess_exec"), "token must be refreshed BEFORE git push"

    def test_create_pr_refreshes_before_gh_runs(self):
        from minions.agents.tools import mcp_executor

        body = self._body(mcp_executor, "_create_pr")

        assert "refresh_env_token" in body, "_create_pr shells out to gh without refreshing the token"

    def test_refresh_is_a_noop_without_app_auth(self):
        """Local dev uses a static PAT. Refresh must not blow up there."""
        import asyncio

        from minions.providers import github_app

        github_app.reset_token_provider()

        assert asyncio.run(github_app.refresh_env_token()) is None

    def test_refresh_never_raises_when_github_is_unreachable(self):
        """A git command must fail with git's error, not with a refresh error."""
        import asyncio

        from minions.providers import github_app

        class _Boom:
            async def token(self):
                raise github_app.GitHubAppError("network is down")

        original = github_app._provider
        github_app._provider = _Boom()
        try:
            assert asyncio.run(github_app.refresh_env_token()) is None
        finally:
            github_app._provider = original


class TestRefreshIsNotCoupledToThePollLoop:
    def test_token_refresh_runs_on_its_own_task(self):
        """It was the first statement of each poll cycle, so a stalled poll
        silently expired the credential."""
        from minions.engine import job_engine

        src = Path(job_engine.__file__).read_text(encoding="utf-8")

        assert "_token_refresh_loop" in src, "no independent token refresh task"
        assert 'name="token-refresh"' in src, "refresh loop is not spawned as its own task"

    def test_every_ensure_token_call_lives_in_the_refresh_loop(self):
        """Positional, not windowed. An earlier version of this test scanned a
        fixed 400 characters after `while self._running:` and passed against the
        unfixed code, because the comment block above the call was longer than
        the window -- a green test for a bug that was still there."""
        import re

        from minions.engine import job_engine

        src = Path(job_engine.__file__).read_text(encoding="utf-8")

        assert "async def _token_refresh_loop" in src, "no independent refresh loop to hold the call"
        refresh_start = src.index("async def _token_refresh_loop")

        calls = [m.start() for m in re.finditer(r"await ensure_token\(", src)]

        assert calls, "ensure_token is never called at all"
        assert all(pos > refresh_start for pos in calls), (
            "ensure_token is still called outside _token_refresh_loop (the poll loop gates the token again)"
        )

    def test_the_refresh_loop_survives_a_failure(self):
        from minions.engine import job_engine

        src = Path(job_engine.__file__).read_text(encoding="utf-8")
        body = src[src.index("async def _token_refresh_loop") :]
        body = body[: body.index("\n    def ") if "\n    def " in body else len(body)]

        assert "except Exception" in body, "an exception would end the loop and restore silent expiry"


class TestAutoRetryIsNotCancelledByAStaleSnapshot:
    """The retry was scheduled and the job failed in the same second."""

    def _manage_source(self) -> str:
        from minions.engine import dev

        src = Path(dev.__file__).read_text(encoding="utf-8")
        start = src.index("async def manage_dev_tasks")
        return src[start:]

    def test_a_requeue_is_tracked(self):
        src = self._manage_source()

        assert "requeued = True" in src, "nothing records that a task went back to PENDING"

    def test_the_terminal_check_is_skipped_after_a_requeue(self):
        """dev_tasks is a snapshot; a requeued task still reads FAILED in it."""
        src = self._manage_source()

        guard = src.index("if requeued:")
        terminal = src.index("all_terminal = all(")

        assert guard < terminal, "the stale snapshot is still evaluated after a requeue"

    def test_both_reset_paths_set_the_flag(self):
        """Auto-retry and orphan recovery both put a task back to PENDING."""
        src = self._manage_source()

        assert src.count("requeued = True") >= 2, "only one of the two requeue paths is covered"

    def test_pending_is_not_a_terminal_status(self):
        """The premise: a requeued task is genuinely non-terminal, so failing
        the job on it is wrong rather than merely early."""
        terminal = {TaskStatus.MERGED, TaskStatus.DONE, TaskStatus.FAILED}

        assert TaskStatus.PENDING not in terminal


class TestCompletedAgentsAreNotReportedStale:
    def test_the_query_excludes_agents_that_have_finished(self):
        """A finished agent stops heartbeating. Without the agents join it is
        marked lost, logged, and sent a kill signal."""
        from minions.db import postgres

        src = Path(postgres.__file__).read_text(encoding="utf-8")
        start = src.index("async def get_stale_heartbeats")
        body = src[start : src.index("\n    async def ", start + 1)]

        assert "JOIN" in body.upper(), "get_stale_heartbeats does not consult agent status"
        assert "'starting', 'running'" in body, "the join does not restrict to live agents"

    def test_orphan_heartbeats_are_still_reported(self):
        """An agent row that vanished must not silently disable detection."""
        from minions.db import postgres

        src = Path(postgres.__file__).read_text(encoding="utf-8")
        start = src.index("async def get_stale_heartbeats")
        body = src[start : src.index("\n    async def ", start + 1)]

        assert "a.id IS NULL" in body, "a heartbeat with no agent row would be dropped from detection"
