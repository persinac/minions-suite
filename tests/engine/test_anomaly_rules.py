"""The stuck-task rule must not read a herder's silence as death.

External herders create no subtasks and send no heartbeats — fifteen quiet
minutes is a healthy herder's normal profile. Job 7ba724fd lost all three of
its attempts to this rule in one evening (23:00Z and 23:27Z firings, plus the
fallback the first one armed) while its herder was demonstrably mid-work,
running pytest in its worktree. Job 3b8b8ba9 died the same way on 08-21:
attempt 2 was retried away without any agent ever having run.

The division of labor these tests pin down:

- a LIVE herder claim is activity — hands off to herder_work_timeout_seconds
  (the age-based stale-claim detector in engine/dev.py), never retried here
- an UNCLAIMED external work item is a wait, owned by
  herder_claim_timeout_seconds — never retried here
- a dead in-process agent is still exactly what this rule exists to catch
"""

from minions.core.models import Agent, AgentRole, JobStatus, Task, TaskStatus
from minions.engine.anomaly_rules import check_stuck_tasks

# elapsed >= 0 is always true, so every in_progress task counts as "old
# enough" without manufacturing stale timestamps.
ALWAYS_STALE = 0


async def _dev_job_with_task(db, status=TaskStatus.IN_PROGRESS):
    job = await db.create_job("spec")
    for s in (JobStatus.SPEC_READY, JobStatus.TASKS_CREATED, JobStatus.DEV_IN_PROGRESS):
        await db.update_job_status(job.id, s)
    task = await db.create_task(Task(job_id=job.id, title="t", description="d", service="api", agent_role=AgentRole.BACKEND_ENGINEER))
    await db.update_task(task.id, status=TaskStatus.IN_PROGRESS)
    if status != TaskStatus.IN_PROGRESS:
        await db.update_task(task.id, pr_url="https://github.com/o/r/pull/1", pr_number=1, branch_name="feat/x")
        await db.update_task(task.id, status=TaskStatus.PR_OPEN)
        await db.update_task(task.id, status=status)
    return job, await db.get_task(task.id)


async def _agent_on(db, job, task, model, status="running"):
    agent = await db.create_agent(Agent(job_id=job.id, role=AgentRole.BACKEND_ENGINEER, task_id=task.id, model=model))
    await db.update_agent(agent.id, status=status)
    return agent


def _stuck_task_ids(anomalies):
    return {a.entity_id for a in anomalies if a.rule_name == "stuck_task"}


class TestLiveHerderClaims:
    async def test_a_live_herder_claim_is_not_stuck(self, db):
        job, task = await _dev_job_with_task(db)
        await _agent_on(db, job, task, model="herder:herder-w19p9")

        anomalies = await check_stuck_tasks(db, ALWAYS_STALE, "external")

        assert task.id not in _stuck_task_ids(anomalies)

    async def test_a_starting_herder_claim_is_not_stuck_either(self, db):
        job, task = await _dev_job_with_task(db)
        await _agent_on(db, job, task, model="herder:herder-w3", status="starting")

        anomalies = await check_stuck_tasks(db, ALWAYS_STALE, "external")

        assert task.id not in _stuck_task_ids(anomalies)

    async def test_a_dead_herder_claim_is_left_to_the_stale_claim_detector(self, db):
        """Once herder_work_timeout marks the agent failed, the engine's own
        recovery owns the task — a retry from here would double-remediate."""
        job, task = await _dev_job_with_task(db)
        await _agent_on(db, job, task, model="herder:herder-w19p9", status="failed")

        anomalies = await check_stuck_tasks(db, ALWAYS_STALE, "external")

        assert task.id not in _stuck_task_ids(anomalies)


class TestUnclaimedExternalWork:
    async def test_an_unclaimed_external_item_is_a_wait_not_a_stall(self, db):
        _job, task = await _dev_job_with_task(db)

        anomalies = await check_stuck_tasks(db, ALWAYS_STALE, "external")

        assert task.id not in _stuck_task_ids(anomalies)

    async def test_the_same_state_under_in_process_dispatch_is_still_stuck(self, db):
        """An in_progress task with no agent and no dispatch queue behind it
        really is orphaned — the rule must keep catching that."""
        _job, task = await _dev_job_with_task(db)

        anomalies = await check_stuck_tasks(db, ALWAYS_STALE, "in_process")

        assert task.id in _stuck_task_ids(anomalies)


class TestTheRuleStillCatchesRealStalls:
    async def test_a_silent_in_process_agent_is_still_stuck(self, db):
        """The metered path emits subtasks and can heartbeat; silence there is
        the real signal this rule exists for — even under external dispatch,
        where the in-process fallback agent runs."""
        job, task = await _dev_job_with_task(db)
        await _agent_on(db, job, task, model="claude-sonnet-5")

        anomalies = await check_stuck_tasks(db, ALWAYS_STALE, "external")

        assert task.id in _stuck_task_ids(anomalies)

    async def test_in_review_handling_is_unchanged(self, db):
        """The herder carve-outs are scoped to in_progress. A silent in_review
        task keeps its existing remediation (reset to pr_open) regardless of
        dispatch mode."""
        _job, task = await _dev_job_with_task(db, status=TaskStatus.IN_REVIEW)

        anomalies = await check_stuck_tasks(db, ALWAYS_STALE, "external")

        assert task.id in _stuck_task_ids(anomalies)

    async def test_default_dispatch_preserves_the_old_behavior(self, db):
        _job, task = await _dev_job_with_task(db)

        anomalies = await check_stuck_tasks(db, ALWAYS_STALE)

        assert task.id in _stuck_task_ids(anomalies)
