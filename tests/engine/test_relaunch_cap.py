"""An orchestration role that never advances the job must stop, not loop.

`launch_spec_analyst` and `launch_arbiter` are driven by the job's STATUS, not by
an attempt counter. The poll loop sees `spec_received` and launches an analyst --
every poll, forever. `_has_running_agent` refuses a concurrent second one; until
this cap, nothing refused a sequential seventh.

Job 4922beee is the case these tests reconstruct. The deployed database was
missing a column `update_job_spec` writes, so every analyst ran to completion,
failed to advance the job, and was relaunched. Seven agents, $0.95, no progress,
stopped by hand. `job_cost_limit_usd` at $25 was the only real ceiling, and it
would have taken all of it.

The lesson these pin is that the failure was NOT a bad agent -- retrying the
agent could never have fixed a missing column. A relaunch loop is only ever
correct when the retry can plausibly succeed, and status-driven relaunch cannot
tell the difference.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from minions.core.models import Agent, AgentRole, Job, JobStatus
from minions.engine.dev import _relaunch_budget_spent


def _engine(agents: list[Agent], cap: int = 3):
    engine = MagicMock()
    engine.config = MagicMock()
    engine.config.orchestration_max_attempts = cap
    engine.db = MagicMock()
    engine.db.get_agents_for_job = AsyncMock(return_value=agents)
    engine.db.record_event = AsyncMock()
    engine.db.update_job_status = AsyncMock()
    engine._on_job_terminal = AsyncMock()
    return engine


def _agent(role: AgentRole, cost: float = 0.1) -> Agent:
    return Agent(job_id="j1", role=role, model="claude-haiku-4-5", status="done", cost_usd=cost)


@pytest.fixture
def job():
    return Job(id="j1", spec="do a thing", status=JobStatus.SPEC_RECEIVED)


class TestUnderTheCap:
    async def test_a_first_launch_is_allowed(self, job):
        engine = _engine([])

        assert await _relaunch_budget_spent(engine, job, AgentRole.SPEC_ANALYST, "Spec analyst") is False
        engine.db.update_job_status.assert_not_called()

    async def test_retries_below_the_cap_are_allowed(self, job):
        """A genuinely flaky agent should get another go."""
        engine = _engine([_agent(AgentRole.SPEC_ANALYST), _agent(AgentRole.SPEC_ANALYST)])

        assert await _relaunch_budget_spent(engine, job, AgentRole.SPEC_ANALYST, "Spec analyst") is False

    async def test_other_roles_do_not_count_against_this_one(self, job):
        """Every job has an analyst and an arbiter; a shared counter would fail
        healthy jobs on their first arbiter launch."""
        engine = _engine([_agent(AgentRole.SPEC_ANALYST) for _ in range(5)])

        assert await _relaunch_budget_spent(engine, job, AgentRole.ARBITER, "Arbiter") is False


class TestAtTheCap:
    async def test_the_job_is_failed(self, job):
        engine = _engine([_agent(AgentRole.SPEC_ANALYST) for _ in range(3)])

        assert await _relaunch_budget_spent(engine, job, AgentRole.SPEC_ANALYST, "Spec analyst") is True

        engine.db.update_job_status.assert_awaited_once()
        assert engine.db.update_job_status.await_args.args[1] == JobStatus.FAILED

    async def test_the_error_says_what_happened_and_what_it_cost(self, job):
        """ "Job failed" sends someone to the logs. The count and the spend are
        the two facts that identify this as a loop rather than a bad agent."""
        engine = _engine([_agent(AgentRole.SPEC_ANALYST, cost=0.5) for _ in range(3)])

        await _relaunch_budget_spent(engine, job, AgentRole.SPEC_ANALYST, "Spec analyst")

        error = engine.db.update_job_status.await_args.kwargs["error"]
        assert "3 times" in error
        assert "1.50" in error, "the money spent looping belongs in the error"
        assert "spec_received" in error, "the state it could not leave is the diagnosis"

    async def test_it_records_an_event(self, job):
        engine = _engine([_agent(AgentRole.SPEC_ANALYST) for _ in range(3)])

        await _relaunch_budget_spent(engine, job, AgentRole.SPEC_ANALYST, "Spec analyst")

        assert engine.db.record_event.await_args.args[1] == "relaunch_cap_reached"

    async def test_terminal_cleanup_runs(self, job):
        """Without this the job stays in the active set and keeps being polled."""
        engine = _engine([_agent(AgentRole.SPEC_ANALYST) for _ in range(3)])

        await _relaunch_budget_spent(engine, job, AgentRole.SPEC_ANALYST, "Spec analyst")

        engine._on_job_terminal.assert_awaited_once()

    async def test_the_arbiter_is_capped_too(self, job):
        """It has the same status-driven shape; capping only the analyst moves
        the loop one stage later rather than removing it."""
        job.status = JobStatus.SPEC_READY
        engine = _engine([_agent(AgentRole.ARBITER) for _ in range(3)])

        assert await _relaunch_budget_spent(engine, job, AgentRole.ARBITER, "Arbiter") is True


class TestDisabling:
    async def test_zero_disables_the_cap(self, job):
        """An escape hatch for a deployment that would rather keep retrying."""
        engine = _engine([_agent(AgentRole.SPEC_ANALYST) for _ in range(50)], cap=0)

        assert await _relaunch_budget_spent(engine, job, AgentRole.SPEC_ANALYST, "Spec analyst") is False
        engine.db.update_job_status.assert_not_called()


class TestTheLoopItReplaces:
    async def test_job_4922beee_would_have_been_stopped_at_three(self, job):
        """The reconstruction: seven analysts, none advancing the job.

        Under the cap the fourth launch never happens, so the job fails at three
        agents instead of seven — and would have kept going to the $25 job limit
        had nobody been watching.
        """
        launched: list[Agent] = []
        engine = _engine(launched)

        stopped_at = None
        for attempt in range(1, 8):
            if await _relaunch_budget_spent(engine, job, AgentRole.SPEC_ANALYST, "Spec analyst"):
                stopped_at = attempt
                break
            launched.append(_agent(AgentRole.SPEC_ANALYST, cost=0.135))

        assert stopped_at == 4, "the fourth launch attempt must be refused"
        assert len(launched) == 3
