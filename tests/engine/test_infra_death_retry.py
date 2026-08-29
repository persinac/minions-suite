"""A rollout must not spend a task's attempt budget.

An engine restart kills every in-process agent on the way down (JobEngine.stop)
and orphans whatever it missed on the way back up (_startup_cleanup). Both paths
used to charge the task an attempt, so three rollouts failed a task whose work
was never at fault. On 2026-08-29 that was visible in the data as
"max attempts reached after agent death" — 6 of ~21 failed tasks — and jobs
touched by an agent death carried $20.83 of the $43.72 in failed spend.

The attempt budget exists to stop an agent that cannot do the job. A pod that
went away is not evidence of that, so an infrastructural death now requeues
without incrementing `attempt`.

The budget is separate rather than unlimited because a PERMANENT fault also
kills at turn 0: an AuthenticationError would otherwise requeue forever. Hence
both halves of the rule — a narrow marker list, and a cap.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from minions.core.models import TaskStatus
from minions.engine.dev import count_infra_deaths, is_infrastructure_death
from minions.engine.job_engine import JobEngine


class _Agent:
    def __init__(self, *, status="failed", error=None, num_turns=0, task_id="t1", agent_id="a1"):
        self.id = agent_id
        self.status = status
        self.error = error
        self.num_turns = num_turns
        self.task_id = task_id
        self.job_id = "j1"
        self.k8s_job_name = None
        self.role = "backend_engineer"


class TestIsInfrastructureDeath:
    """The predicate both recovery paths share."""

    @pytest.mark.parametrize(
        "error",
        [
            "interrupted by engine shutdown",
            "orphaned by restart",
            "orphaned by restart (k8s disabled)",
            "k8s job not found after restart",
            "litellm.APIConnectionError: AnthropicException",
            "stuck in starting state",
        ],
    )
    def test_platform_faults_at_turn_zero_are_infrastructural(self, error):
        assert is_infrastructure_death(_Agent(error=error)) is True

    def test_work_done_means_it_costs_an_attempt_whatever_killed_it(self):
        """The load-bearing half.

        An agent that took turns may have left a half-finished branch behind, so
        the next attempt is cleaning up rather than starting fresh. That is worth
        an attempt even when a rollout is what stopped it.
        """
        assert is_infrastructure_death(_Agent(error="interrupted by engine shutdown", num_turns=1)) is False

    @pytest.mark.parametrize(
        "error",
        [
            "litellm.AuthenticationError: OpenAIException",
            "litellm.BadRequestError: AnthropicException",
            "agent died without completing",
            None,
        ],
    )
    def test_permanent_or_unknown_causes_are_not_free(self, error):
        """Retrying a misconfigured model learns nothing, so it must cost something."""
        assert is_infrastructure_death(_Agent(error=error)) is False

    def test_a_live_or_finished_agent_is_not_a_death(self):
        assert is_infrastructure_death(_Agent(status="running", error="interrupted by engine shutdown")) is False
        assert is_infrastructure_death(_Agent(status="done", error="interrupted by engine shutdown")) is False


@pytest.mark.asyncio
class TestCountInfraDeaths:
    async def _db(self, agents):
        db = MagicMock()
        db.get_agents_for_job = AsyncMock(return_value=agents)
        return db

    async def test_counts_only_this_task_and_only_infra_deaths(self):
        db = await self._db(
            [
                _Agent(error="orphaned by restart", task_id="t1"),
                _Agent(error="orphaned by restart", task_id="t2"),  # other task
                _Agent(error="agent died without completing", task_id="t1"),  # not infra
                _Agent(error="interrupted by engine shutdown", task_id="t1", num_turns=4),  # did work
                _Agent(error="interrupted by engine shutdown", task_id="t1"),
            ]
        )
        assert await count_infra_deaths(db, "j1", "t1") == 2

    async def test_a_broken_query_denies_the_free_retry_rather_than_granting_it(self):
        """Fail closed: a bookkeeping outage must not hand out unlimited retries."""
        db = MagicMock()
        db.get_agents_for_job = AsyncMock(side_effect=RuntimeError("db down"))
        assert await count_infra_deaths(db, "j1", "t1") > 10**6


@pytest.mark.asyncio
class TestStartupCleanupPreservesAttempt:
    """The rollout path: restart orphans an agent, task must keep its budget."""

    def _engine(self, *, task_attempt=1, prior_agents=None, max_infra_retries=3):
        e = JobEngine.__new__(JobEngine)
        e.db = MagicMock()
        task = MagicMock()
        task.id = "t1"
        task.status = TaskStatus.IN_PROGRESS
        task.attempt = task_attempt
        task.max_attempts = 3

        orphaned = _Agent(status="running", error=None)
        e.db.get_running_agents = AsyncMock(return_value=[orphaned])
        e.db.update_agent = AsyncMock()
        e.db.record_event = AsyncMock()
        e.db.get_task = AsyncMock(return_value=task)
        e.db.update_task = AsyncMock()
        e.db.clear_all_heartbeats = AsyncMock()
        # What the agent looks like AFTER _startup_cleanup marks it orphaned.
        e.db.get_agent = AsyncMock(return_value=_Agent(error="orphaned by restart"))
        e.db.get_agents_for_job = AsyncMock(return_value=prior_agents if prior_agents is not None else [_Agent(error="orphaned by restart")])
        e.config = MagicMock()
        e.config.max_infra_retries = max_infra_retries
        # _k8s_enabled is a read-only property derived from these two.
        e.config.k8s_dispatch = False
        e._k8s_launcher = None
        e._log_reconciliation = AsyncMock()
        return e, task

    def _requeue_kwargs(self, engine):
        return [c.kwargs for c in engine.db.update_task.call_args_list if c.kwargs.get("status") == TaskStatus.PENDING]

    async def test_restart_requeues_without_spending_an_attempt(self):
        engine, _ = self._engine(task_attempt=1)
        await engine._startup_cleanup()

        [requeue] = self._requeue_kwargs(engine)
        assert "attempt" not in requeue, "a rollout must not cost the task an attempt"
        events = [c.args[1] for c in engine.db.record_event.call_args_list]
        assert "task_infra_requeue" in events, "a free retry must be visible, or it cannot be audited"

    async def test_the_last_attempt_still_survives_a_restart(self):
        """Previously the case that failed the job: attempt 3 of 3, killed by a rollout."""
        engine, _ = self._engine(task_attempt=3)
        await engine._startup_cleanup()

        [requeue] = self._requeue_kwargs(engine)
        assert "attempt" not in requeue
        failed = [c.kwargs for c in engine.db.update_task.call_args_list if c.kwargs.get("status") == TaskStatus.FAILED]
        assert all("max attempts reached" not in (k.get("error") or "") for k in failed)

    async def test_repeated_restarts_stop_being_free_at_the_cap(self):
        """Otherwise a permanent turn-0 fault requeues forever."""
        engine, _ = self._engine(task_attempt=1, prior_agents=[_Agent(error="orphaned by restart", agent_id=f"a{i}") for i in range(4)])
        await engine._startup_cleanup()

        [requeue] = self._requeue_kwargs(engine)
        assert requeue.get("attempt") == 2, "past the cap, a restart costs an attempt again"

    async def test_a_non_infrastructural_death_still_costs_an_attempt(self):
        """Negative control — without it the change could make every death free."""
        engine, _ = self._engine(task_attempt=1)
        engine.db.get_agent = AsyncMock(return_value=_Agent(error="litellm.AuthenticationError"))
        await engine._startup_cleanup()

        [requeue] = self._requeue_kwargs(engine)
        assert requeue.get("attempt") == 2


class TestPollLoopRecoveryBranch:
    """The second recovery path, in dev.manage_dev_tasks.

    Reached only through a long poll-loop function, so it is guarded
    structurally rather than by driving the whole loop — the same approach
    test_retry_accounting.py uses for its neighbours.
    """

    def _free_retry_block(self) -> str:
        import inspect

        from minions.engine import dev

        source = inspect.getsource(dev.manage_dev_tasks)
        start = source.index("if free_retry:")
        end = source.index("elif task.attempt < task.max_attempts:", start)
        return source[start:end]

    def test_the_free_retry_requeues_without_touching_attempt(self):
        # Scoped to the update_task calls on purpose: the event payload in this
        # block legitimately contains the text "attempt={task.attempt}", so a
        # naive substring check over the whole block reports a false positive.
        writes = [line for line in self._free_retry_block().splitlines() if "update_task" in line]

        assert any("status=TaskStatus.PENDING" in line for line in writes), "the block must actually requeue the task"
        assert all("attempt=" not in line for line in writes), "an infrastructural death must not consume an attempt"

    def test_the_free_retry_is_gated_on_the_shared_predicate_and_the_cap(self):
        import inspect

        from minions.engine import dev

        source = inspect.getsource(dev.manage_dev_tasks)

        assert "is_infrastructure_death(latest_agent)" in source
        assert "engine.config.max_infra_retries" in source, "an ungated free retry loops forever on a permanent fault"
