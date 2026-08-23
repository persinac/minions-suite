"""The cost report must count the workload that exists.

`minion --costs` filtered on job_type = 'review' — a type of which zero jobs
have ever existed — so a deployment whose entire workload was dev jobs read
$0.0000 forever. And "avg per review" was AVG over agent ROWS: a job with a
cheap heartbeat agent and an expensive engineer averaged to something no job
ever cost. The summary now counts every job type and divides real dollars by
real jobs.
"""

import inspect

from minions.core.models import Agent, AgentRole, Task


async def _dev_job_with_spend(db, cost: float):
    job = await db.create_job("spec")
    task = await db.create_task(Task(job_id=job.id, title="t", description="d", service="api", agent_role=AgentRole.BACKEND_ENGINEER))
    agent = await db.create_agent(Agent(job_id=job.id, role=AgentRole.BACKEND_ENGINEER, task_id=task.id, model="claude-sonnet-5"))
    await db.update_agent(agent.id, cost_usd=cost, input_tokens=1000, output_tokens=500)
    return job


class TestDevJobsAreCounted:
    async def test_a_dev_job_and_its_spend_appear(self, db):
        await _dev_job_with_spend(db, 2.5)

        summary = await db.get_cost_summary()

        assert summary["total_jobs"] >= 1
        assert summary["dev_jobs"] >= 1
        assert summary["total_cost_usd"] >= 2.5
        assert summary["total_input_tokens"] >= 1000

    async def test_avg_is_dollars_per_job_not_per_agent_row(self, db):
        """One job, two agents ($3 + $1): the average must be $4/job, not the
        $2/agent-row the old AVG() produced."""
        job = await _dev_job_with_spend(db, 3.0)
        task = await db.create_task(Task(job_id=job.id, title="t2", description="d", service="api", agent_role=AgentRole.BACKEND_ENGINEER))
        second = await db.create_agent(Agent(job_id=job.id, role=AgentRole.BACKEND_ENGINEER, task_id=task.id, model="claude-sonnet-5"))
        await db.update_agent(second.id, cost_usd=1.0)

        summary = await db.get_cost_summary()

        per_job = summary["total_cost_usd"] / summary["total_jobs"]
        assert abs(summary["avg_cost_per_job"] - per_job) < 0.01

    async def test_the_review_filter_is_gone_from_the_where_clause(self, db):
        """job_type='review' may appear only inside the FILTER that splits the
        count — never as the query's WHERE, which is what excluded every job
        that has ever existed."""
        source = inspect.getsource(type(db).get_cost_summary)

        stripped = source.replace("FILTER (WHERE j.job_type = 'review')", "")
        assert "j.job_type = 'review'" not in stripped
        assert "total_jobs" in source


class TestDashboardCountsHonestly:
    def test_no_work_needed_is_not_active(self):
        """A terminal status counted as Active forever — the same bug class
        the engine fixed at deploy.py's TERMINAL_DEV_TASK_STATUSES; the
        dashboard copy was missed."""
        import minions.dashboard as dashboard

        source = inspect.getsource(dashboard)
        active_predicates = source.count("NOT IN ('done','failed','no_work_needed')")
        assert active_predicates == 2, "both Active counters (page render + HTMX endpoint) must exclude all three terminal states"
        assert "NOT IN ('done','failed')\"" not in source

    def test_no_work_needed_has_a_badge_color(self):
        from minions.dashboard import STATUS_CSS

        assert "no_work_needed" in STATUS_CSS

    def test_the_job_list_is_bounded(self):
        import minions.dashboard as dashboard

        source = inspect.getsource(dashboard._render_job_list)
        assert "LIMIT 100" in source, "the list re-renders on a 5s poll; unbounded means every job ever, every 5 seconds"
        assert "job_costs" in source, "per-job spend comes from one aggregate query, not another per-row query"
