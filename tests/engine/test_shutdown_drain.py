"""A rollout must not throw away an agent that is nearly done.

stop() marked every in-process agent failed the instant it was called. Every
rollout SIGTERMs the pod for reasons that have nothing to do with the job inside
it — an image bump, a config change, an ArgoCD sync — so an engineer fifteen
minutes and a couple of dollars into a run lost all of it, and the job restarted
from zero on the next poll. The pod's grace period was the k8s default of 30s,
so even a willing agent had no room.

stop() now drains first: it stops dispatching, waits for in-flight in-process
agents up to shutdown_grace_seconds, and only then fails whatever is left.

What this does NOT do is survive the process — the LLM conversation is in
memory. It buys the common case, an agent a turn or two from committing, and it
composes with the engineer prompt's branch-first / commit-per-subtask ordering
so a drained agent leaves recoverable work rather than a dirty tree.

K8s agents are excluded throughout: they run in their own pods and outlive this
one on purpose.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from minions.core.models import Agent
from minions.engine.job_engine import JobEngine


def _agent(agent_id: str, k8s: str | None = None) -> Agent:
    a = Agent(id=agent_id, model="claude-opus-5", status="running")
    a.k8s_job_name = k8s
    return a


@pytest.fixture
def engine():
    e = JobEngine.__new__(JobEngine)
    e.db = MagicMock()
    e.db.get_running_agents = AsyncMock(return_value=[])
    e.db.update_agent = AsyncMock()
    e.db.record_event = AsyncMock()
    e.config = MagicMock()
    e.config.shutdown_grace_seconds = 300.0
    e._running = True
    e._background_tasks = set()
    return e


class TestDrainWaits:
    async def test_it_returns_as_soon_as_the_last_agent_finishes(self, engine):
        calls = {"n": 0}

        async def running(*_a, **_k):
            calls["n"] += 1
            if calls["n"] < 3:
                return [_agent("a1")]
            return []

        engine.db.get_running_agents = AsyncMock(side_effect=running)

        await asyncio.wait_for(engine._drain_in_process_agents(30.0), timeout=15)

        assert calls["n"] >= 3, "it must keep polling until the agent clears"

    async def test_it_gives_up_at_the_grace_period(self, engine):
        """A stuck agent delays a rollout; it must not block one forever."""
        engine.db.get_running_agents = AsyncMock(return_value=[_agent("a1")])

        await asyncio.wait_for(engine._drain_in_process_agents(3.0), timeout=20)

    async def test_zero_grace_returns_immediately(self, engine):
        """The opt-out has to be real — some callers want the old behaviour."""
        engine.db.get_running_agents = AsyncMock(return_value=[_agent("a1")])

        await engine._drain_in_process_agents(0)

        engine.db.get_running_agents.assert_not_called()

    async def test_k8s_agents_do_not_hold_the_drain_open(self, engine):
        """They run in their own pods and are meant to outlive this one."""
        engine.db.get_running_agents = AsyncMock(return_value=[_agent("k1", k8s="minion-job-k1")])

        await asyncio.wait_for(engine._drain_in_process_agents(30.0), timeout=10)

    async def test_a_db_failure_does_not_hold_the_process_open(self, engine):
        """Shutdown must complete even when the database is the thing broken."""
        engine.db.get_running_agents = AsyncMock(side_effect=RuntimeError("pg gone"))

        await asyncio.wait_for(engine._drain_in_process_agents(30.0), timeout=10)


class TestStopOrdering:
    async def test_dispatch_stops_before_the_drain_begins(self, engine):
        """Otherwise new agents launch into a draining engine and the wait never
        converges."""
        seen = {}

        async def running(*_a, **_k):
            seen["running_flag"] = engine._running
            return []

        engine.db.get_running_agents = AsyncMock(side_effect=running)

        await asyncio.wait_for(engine.stop(grace_seconds=5.0), timeout=20)

        assert seen["running_flag"] is False

    async def test_survivors_are_still_marked_failed(self, engine):
        """The drain is a grace period, not an amnesty — anything still running
        afterwards must not be left looking alive in the database."""
        engine.db.get_running_agents = AsyncMock(return_value=[_agent("a1")])

        await asyncio.wait_for(engine.stop(grace_seconds=2.0), timeout=20)

        engine.db.update_agent.assert_called_once()
        agent_id, kwargs = engine.db.update_agent.call_args[0][0], engine.db.update_agent.call_args.kwargs
        assert agent_id == "a1"
        assert kwargs["status"] == "failed"
        assert kwargs["error"] == "interrupted by engine shutdown"

    async def test_k8s_survivors_are_left_alone(self, engine):
        engine.db.get_running_agents = AsyncMock(return_value=[_agent("k1", k8s="minion-job-k1")])

        await asyncio.wait_for(engine.stop(grace_seconds=1.0), timeout=20)

        engine.db.update_agent.assert_not_called()

    async def test_it_defaults_to_the_configured_grace(self, engine):
        """cli.py calls stop() with no argument on every shutdown path."""
        import inspect

        source = inspect.getsource(JobEngine.stop)

        assert "self.config.shutdown_grace_seconds" in source


class TestPodGracePeriodCoversTheDrain:
    def test_termination_grace_exceeds_the_engine_grace(self):
        """If the kubelet SIGKILLs mid-drain the whole mechanism is theatre."""
        import re
        from pathlib import Path

        manifest = Path(__file__).resolve().parents[2] / "k8s" / "base" / "minion-suite" / "deployment.yaml"
        pod_grace = int(re.search(r"terminationGracePeriodSeconds:\s*(\d+)", manifest.read_text()).group(1))

        settings = Path(__file__).resolve().parents[2] / "settings.toml"
        engine_grace = float(re.search(r"^shutdown_grace_seconds\s*=\s*([\d.]+)", settings.read_text(), re.M).group(1))

        assert pod_grace > engine_grace, f"pod grace {pod_grace}s must exceed engine drain {engine_grace}s"
