"""Spend ceilings.

Nothing bounded cost before these existed. The agent loop stopped on turns or
wall-clock only, and max_turns was hardcoded at 100 — one backend_engineer run
billed $20.57 by turn 64 with headroom for roughly half as much again, and a
duplicate reviewer added $2.50 nobody asked for.

Two independent ceilings, because they fail differently:
  * per-agent, enforced inside the tool-use loop — bounds one runaway agent
  * per-job, checked before each agent launches — bounds a job that keeps
    launching agents that are each individually under budget
"""

import pytest

from minions.config import Config


class TestConfigDefaults:
    def test_limits_are_on_by_default(self, monkeypatch):
        """A ceiling nobody enabled is not a ceiling."""
        for key in ("AGENT_COST_LIMIT_USD", "JOB_COST_LIMIT_USD", "AGENT_MAX_TURNS"):
            monkeypatch.delenv(key, raising=False)

        config = Config.from_env()

        assert config.agent_cost_limit_usd > 0
        assert config.job_cost_limit_usd > 0
        assert config.agent_max_turns > 0

    def test_job_limit_exceeds_agent_limit(self):
        """A job must afford at least one full agent, or nothing can ever run."""
        config = Config.from_env()

        assert config.job_cost_limit_usd > config.agent_cost_limit_usd

    def test_limits_are_env_overridable(self, monkeypatch):
        monkeypatch.setenv("AGENT_COST_LIMIT_USD", "1.25")
        monkeypatch.setenv("JOB_COST_LIMIT_USD", "3.50")
        monkeypatch.setenv("AGENT_MAX_TURNS", "7")

        config = Config.from_env()

        assert config.agent_cost_limit_usd == 1.25
        assert config.job_cost_limit_usd == 3.50
        assert config.agent_max_turns == 7

    def test_zero_disables_a_limit(self, monkeypatch):
        """0 is the documented escape hatch for an unbounded run."""
        monkeypatch.setenv("AGENT_COST_LIMIT_USD", "0")

        assert Config.from_env().agent_cost_limit_usd == 0


class TestPerJobCeiling:
    """_run_in_process must refuse to start an agent on an over-budget job."""

    async def test_refuses_to_launch_when_the_job_is_over_budget(self, db, monkeypatch):
        from minions.core.models import Agent, AgentRole, JobStatus, Task, TaskStatus
        from minions.engine.job_engine import JobEngine

        config = Config.from_env()
        config.job_cost_limit_usd = 5.0

        job = await db.create_job("spend a lot")
        task = await db.create_task(Task(job_id=job.id, title="t", service="svc", agent_role=AgentRole.BACKEND_ENGINEER))

        # Two agents already billed past the ceiling.
        for cost in (3.0, 2.5):
            agent = await db.create_agent(Agent(job_id=job.id, role=AgentRole.BACKEND_ENGINEER, task_id=task.id, model="m"))
            await db.update_agent(agent.id, cost_usd=cost)

        engine = JobEngine(db, config)

        called = False

        async def _should_not_run(*args, **kwargs):
            nonlocal called
            called = True
            raise AssertionError("run_agent must not be called for an over-budget job")

        monkeypatch.setattr("minions.engine.job_engine.run_agent", _should_not_run)

        result = await engine._run_in_process(
            job=job,
            task=task,
            agent=Agent(job_id=job.id, role=AgentRole.BACKEND_ENGINEER, task_id=task.id, model="m"),
            project=None,
            service=None,
        )

        assert result is None
        assert not called, "the expensive call happened anyway"

        # The failure must be recorded, not silently swallowed.
        assert (await db.get_job(job.id)).status == JobStatus.FAILED
        assert (await db.get_task(task.id)).status == TaskStatus.FAILED

    async def _assert_gate_allows(self, db, monkeypatch, limit: float, already_spent: float):
        """Drive _run_in_process only as far as the budget gate.

        run_agent raises a sentinel, so reaching it proves the gate allowed
        execution. Letting the method run to completion would continue into
        NATS and Trello notification, which block on the network in tests.
        """
        from minions.core.models import Agent, AgentRole, Task
        from minions.engine.job_engine import JobEngine

        config = Config.from_env()
        config.job_cost_limit_usd = limit

        job = await db.create_job("spec")
        task = await db.create_task(Task(job_id=job.id, title="t", service="svc", agent_role=AgentRole.BACKEND_ENGINEER))
        agent = await db.create_agent(Agent(job_id=job.id, role=AgentRole.BACKEND_ENGINEER, task_id=task.id, model="m"))
        await db.update_agent(agent.id, cost_usd=already_spent)

        class _Reached(Exception):
            pass

        async def _sentinel(**kwargs):
            raise _Reached

        monkeypatch.setattr("minions.engine.job_engine.run_agent", _sentinel)
        monkeypatch.setattr("minions.engine.job_engine.create_mcp_tool_executor", lambda **kw: None)

        engine = JobEngine(db, config)
        with pytest.raises(_Reached):
            await engine._run_in_process(job=job, task=task, agent=agent, project=None, service=None)

    async def test_allows_a_job_still_under_budget(self, db, monkeypatch):
        await self._assert_gate_allows(db, monkeypatch, limit=100.0, already_spent=1.0)

    async def test_zero_limit_disables_the_check(self, db, monkeypatch):
        """0 must mean unlimited, not blocked."""
        await self._assert_gate_allows(db, monkeypatch, limit=0, already_spent=9999.0)


class TestPerAgentCeilingWiring:
    """The limit must reach the loop that is actually used in production."""

    def test_the_langgraph_path_forwards_cost_limit(self):
        """use_langgraph_agent defaults true, so a limit only on the legacy
        loop would never fire in production."""
        import inspect

        from minions.agents import graph

        source = inspect.getsource(graph.agent_execution_node)
        assert "cost_limit" in source, "the subgraph node drops cost_limit — the ceiling would never apply"

    def test_the_loop_accepts_a_cost_limit(self):
        import inspect

        from minions.agents.runner import _agent_loop_generic

        assert "cost_limit" in inspect.signature(_agent_loop_generic).parameters

    def test_run_agent_sources_limits_from_config(self):
        import inspect

        from minions.agents.runner import run_agent

        source = inspect.getsource(run_agent)
        assert "config.agent_cost_limit_usd" in source
        assert "config.agent_max_turns" in source, "max_turns must be configurable, not hardcoded"
