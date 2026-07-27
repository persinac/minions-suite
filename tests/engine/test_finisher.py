"""The git sequence gets its own cheap agent, because it kept being starved.

Three measured runs of the same ticket wrote all the code and then died before
git — 19cbee54 ($2.71, 2.00M tokens), 67f250f4 ($4.45, 2.79M), dbc956ff ($0.93,
2.65M). dbc956ff spent $0.93 against an $8 ceiling: it ran out of TURNS, not
money. branch/commit/push/create_pr/report_pr sits at the END of the budget the
edits have already consumed, and it is the first thing lost.

The old recovery was to retry the whole engineer: re-read the codebase, re-plan,
re-implement work already sitting on disk, at engineer rates, to reach five
mechanical calls. The finisher does just those five, on the cheap tier, with a
prompt that cannot read or write source files.

The interaction that makes this dangerous is `reset_dirty` (#26). A finisher
runs AFTER its engineer has exited, so no other agent is running and the tree
looks orphaned by every test that fix uses — while the uncommitted files in it
are the engineer's output and the whole reason the finisher exists. Resetting
there would delete the work in the name of protecting it, and it would look like
success: clean tree, nothing to commit, no error anywhere. That case is asserted
below and is the most important test in this file.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from minions.agents.tools.definitions import FINISHER_TOOL_DEFINITIONS, get_tools_for_role
from minions.classifier import resolve_model
from minions.config import Config
from minions.core.models import Agent, AgentRole


def _names(tools) -> set[str]:
    return {t["function"]["name"] for t in tools}


class TestToolSurface:
    def test_it_has_the_whole_git_sequence(self):
        """Missing any one of these strands the work it was sent to rescue."""
        assert {"create_branch", "commit", "push", "create_pr", "report_pr"} <= _names(FINISHER_TOOL_DEFINITIONS)

    def test_report_pr_is_present(self):
        """Nothing downstream happens without it — the PR exists on GitHub but
        the state machine never learns, and the task sits in IN_PROGRESS."""
        assert "report_pr" in _names(FINISHER_TOOL_DEFINITIONS)

    @pytest.mark.parametrize("tool", ["read_file", "write_file", "search_code", "submit_subtask_plan"])
    def test_it_cannot_read_or_edit_the_codebase(self, tool):
        """Withholding these is most of the point: it is what keeps the context
        small, and it makes 'the finisher edited something' impossible rather
        than merely discouraged."""
        assert tool not in _names(FINISHER_TOOL_DEFINITIONS)

    def test_it_keeps_run_command_to_inspect_git(self):
        """It must see what it is committing to write an honest PR body."""
        assert "run_command" in _names(FINISHER_TOOL_DEFINITIONS)

    def test_the_role_resolves_to_this_set(self):
        assert _names(get_tools_for_role("finisher")) == _names(FINISHER_TOOL_DEFINITIONS)

    def test_it_is_much_smaller_than_the_engineer_set(self):
        assert len(FINISHER_TOOL_DEFINITIONS) < len(get_tools_for_role("backend_engineer"))

    def test_schemas_are_shared_with_the_engineer_not_copied(self):
        """Derived by name from ENGINEER_TOOL_DEFINITIONS so a change to
        report_pr's schema reaches both roles. Redeclaring would let the two
        drift, and this is the role that must get report_pr exactly right."""
        eng = {t["function"]["name"]: t for t in get_tools_for_role("backend_engineer")}
        for tool in FINISHER_TOOL_DEFINITIONS:
            name = tool["function"]["name"]
            if name in eng:
                assert tool == eng[name]


class TestModelTier:
    @pytest.fixture
    def config(self):
        c = Config.from_env()
        c.model_easy = "claude-haiku-4-5"
        c.model_medium = "claude-sonnet-5"
        c.model_hard = "claude-opus-5"
        c.model_finisher = ""
        return c

    @pytest.mark.parametrize("difficulty", ["easy", "medium", "hard", None])
    def test_difficulty_never_raises_the_tier(self, config, difficulty):
        """Difficulty describes the ticket; the finisher does not do the ticket.
        `git push` costs the same whether the change was a typo or a rewrite."""
        assert resolve_model(config, difficulty, is_finisher=True) == "claude-haiku-4-5"

    def test_a_project_model_does_not_override_it(self, config):
        """The one override deliberately ignored. A project pinning Opus means
        Opus for its code, not for its plumbing."""
        assert resolve_model(config, "hard", project_model="claude-opus-5", is_finisher=True) == "claude-haiku-4-5"

    def test_an_explicit_override_still_wins(self, config):
        """Escape hatch if a cheap model fumbles `gh pr create`."""
        config.model_finisher = "claude-sonnet-5"
        assert resolve_model(config, "easy", is_finisher=True) == "claude-sonnet-5"

    def test_other_roles_are_unaffected(self, config):
        assert resolve_model(config, "hard") == "claude-opus-5"
        assert resolve_model(config, "hard", is_engineer=True) == "claude-opus-5"


class TestCheckoutSafety:
    """The reset_dirty interaction — the one that can silently destroy work."""

    def _engine(self):
        from minions.engine.job_engine import JobEngine

        e = JobEngine.__new__(JobEngine)
        e.db = MagicMock()
        e.db.get_running_agents = AsyncMock(return_value=[])
        e.config = Config.from_env()
        return e

    def test_the_finisher_is_excluded_from_the_orphan_reset(self):
        """Asserted on the source because reaching this line at runtime needs
        the whole in-process launch path stood up. The property is one
        conjunct — losing it is silent, and the symptom (an empty PR, or none)
        looks like a model failure rather than deleted files."""
        import inspect

        from minions.engine.job_engine import JobEngine

        source = inspect.getsource(JobEngine._run_in_process)

        assert "task.agent_role != AgentRole.FINISHER" in source
        assert "reset_dirty=orphaned" in source

    async def test_ensure_checkout_leaves_a_dirty_tree_when_not_told_to_reset(self, tmp_path):
        """The behaviour the exclusion above relies on."""
        import subprocess

        from minions import repos

        work = tmp_path / "w"
        work.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(work)], check=True, capture_output=True)
        for k, v in (("user.email", "t@t.test"), ("user.name", "t")):
            subprocess.run(["git", "-C", str(work), "config", k, v], check=True, capture_output=True)
        (work / "f.txt").write_text("v1\n")
        subprocess.run(["git", "-C", str(work), "add", "-A"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(work), "commit", "-m", "i"], check=True, capture_output=True)

        bare = tmp_path / "o.git"
        subprocess.run(["git", "clone", "--bare", str(work), str(bare)], check=True, capture_output=True)

        dest = tmp_path / "checkout"
        await repos.ensure_checkout(str(bare), str(dest), "main")
        (dest / "engineer_work.py").write_text("the expensive part\n")

        await repos.ensure_checkout(str(bare), str(dest), "main", reset_dirty=False)

        assert (dest / "engineer_work.py").exists(), "the finisher would have nothing left to commit"


class TestFallbackContract:
    """The finisher is layered in front of retry, never a replacement for it."""

    def _engine(self, agents=None, task=None):
        e = MagicMock()
        e.db = MagicMock()
        e.db.get_agents_for_job = AsyncMock(return_value=agents or [])
        e.db.get_job = AsyncMock(return_value=None)
        e.db.get_task = AsyncMock(return_value=task)
        e.db.create_agent = AsyncMock(side_effect=lambda a: a)
        e.db.record_event = AsyncMock()
        e._nats_agent_status = AsyncMock()
        e.config = Config.from_env()

        # Close the coroutine instead of leaking it: _spawn is what makes the
        # run background, so the test must not execute it, but an un-awaited
        # coroutine warns.
        def _spawn(coro, name=""):
            coro.close()
            return MagicMock()

        e._spawn = MagicMock(side_effect=_spawn)
        return e

    async def test_one_finisher_per_task(self):
        """A finisher that failed will fail the same way again, and the no-PR
        condition that triggered it is still true — without this it re-fires
        forever instead of retrying."""
        from minions.core.models import Task
        from minions.engine.dev import _spawn_finisher

        task = Task(job_id="j1", title="t", description="d", service="svc", agent_role=AgentRole.BACKEND_ENGINEER)
        prior = Agent(job_id="j1", role=AgentRole.FINISHER, task_id=task.id, model="claude-haiku-4-5")
        engine = self._engine(agents=[prior])

        assert await _spawn_finisher(engine, task, "test") is False
        engine._spawn.assert_not_called()

    async def test_an_unrelated_prior_agent_does_not_block_it(self):
        from minions.core.models import Task
        from minions.engine.dev import _spawn_finisher

        task = Task(job_id="j1", title="t", description="d", service="svc", agent_role=AgentRole.BACKEND_ENGINEER)
        other = Agent(job_id="j1", role=AgentRole.BACKEND_ENGINEER, task_id=task.id, model="m")
        engine = self._engine(agents=[other])
        engine.db.get_job = AsyncMock(return_value=None)

        # Declines because the job is missing, not because of the prior agent —
        # it got far enough to look the job up.
        assert await _spawn_finisher(engine, task, "test") is False
        engine.db.get_job.assert_awaited()

    async def test_it_never_raises(self):
        """It sits on the path that decides whether a task advances, retries or
        fails. Throwing here strands the task in IN_PROGRESS with no owner."""
        from minions.core.models import Task
        from minions.engine.dev import _spawn_finisher

        task = Task(job_id="j1", title="t", description="d", service="svc", agent_role=AgentRole.BACKEND_ENGINEER)
        engine = self._engine()
        engine.db.get_agents_for_job = AsyncMock(side_effect=RuntimeError("db gone"))

        assert await _spawn_finisher(engine, task, "test") is False

    async def test_it_declines_when_no_service_resolves(self):
        from minions.core.models import Job, Task
        from minions.engine.dev import _spawn_finisher

        task = Task(job_id="j1", title="t", description="d", service="svc", agent_role=AgentRole.BACKEND_ENGINEER)
        engine = self._engine()
        engine.db.get_job = AsyncMock(return_value=Job(spec="s"))
        engine._resolve_service = MagicMock(return_value=(None, None))

        assert await _spawn_finisher(engine, task, "test") is False
        engine._spawn.assert_not_called()

    async def test_the_agent_runs_in_the_background(self):
        """THE reason this is split. One caller of _try_complete_task is the
        poll loop's orphan recovery, which drives job advancement, review checks
        and deploy monitoring for the whole engine. Awaiting an LLM agent there
        stalls all of it for minutes — which is why run_engineer is spawned too.
        """
        from minions.core.models import Job, Task
        from minions.engine.dev import _spawn_finisher

        task = Task(job_id="j1", title="t", description="d", service="svc", agent_role=AgentRole.BACKEND_ENGINEER)
        engine = self._engine()
        engine.db.get_job = AsyncMock(return_value=Job(spec="s"))
        engine._resolve_service = MagicMock(return_value=(MagicMock(), MagicMock(repo_path="/repos/svc", default_branch="main")))
        engine._run_in_process = AsyncMock()

        assert await _spawn_finisher(engine, task, "test") is True
        engine._spawn.assert_called_once()
        engine._run_in_process.assert_not_awaited(), "the agent must not run inline"

    async def test_a_started_finisher_owns_the_retry(self):
        """_try_complete_task returns without retrying once one is spawned, so
        the spawned run must perform the retry itself on failure — otherwise a
        finisher that cannot open a PR silently strands the task."""
        import inspect

        from minions.engine.dev import _finish_task

        source = inspect.getsource(_finish_task)
        assert "_retry_or_fail" in source

    def test_success_is_judged_by_the_pr_not_the_exit_status(self):
        """An agent can finish cleanly having called nothing. Trusting its exit
        status would advance a task that has no PR."""
        import inspect

        from minions.engine.dev import _finish_task

        source = inspect.getsource(_finish_task)

        assert "refreshed.pr_url" in source
        assert "get_task(task.id)" in source

    def test_the_task_row_keeps_its_real_role(self):
        """The override is on a copy. The row must keep saying backend_engineer:
        retry accounting and service-ownership checks read it."""
        import inspect

        from minions.engine.dev import _finish_task

        source = inspect.getsource(_finish_task)

        assert "task.model_copy(update=" in source
        assert "AgentRole.FINISHER" in source


class TestPromptBudget:
    @pytest.fixture(scope="class")
    def prompt(self) -> str:
        from pathlib import Path

        return (Path(__file__).resolve().parents[2] / "prompts" / "agents" / "finisher.md").read_text()

    def test_it_is_small(self, prompt):
        """~2k tokens was the requirement; the cheap context is the point."""
        assert len(prompt.split()) < 900

    def test_it_forbids_destructive_git(self, prompt):
        """The tree it operates on may hold several dollars of work and is the
        only copy — nothing has been pushed yet."""
        for danger in ["--force", "reset --hard", "checkout -- ."]:
            assert danger in prompt, f"{danger} must be explicitly forbidden"

    def test_it_opens_a_pr_even_when_the_work_looks_wrong(self, prompt):
        """A PR with a caveat gets reviewed. No PR gets nothing."""
        assert "still open the PR" in prompt

    def test_it_is_told_not_to_write_code(self, prompt):
        assert "You do not write code" in prompt

    def test_it_knows_report_pr_is_load_bearing(self, prompt):
        flat = " ".join(prompt.split())
        assert "invisible to the system until it is reported" in flat

    def test_it_handles_already_committed_work(self, prompt):
        """The common cut-short case: commits exist but were never pushed."""
        assert "unpushed" in prompt
