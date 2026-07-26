"""Tests for engine/dev.py — task lifecycle after agent completion."""

from unittest.mock import AsyncMock, MagicMock

from minions.core.models import Agent, AgentRole, Job, JobStatus, Subtask, Task, TaskStatus

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_job(job_id="job-1", status=JobStatus.DEV_IN_PROGRESS):
    return Job(id=job_id, spec="test spec", status=status)


def _make_task(task_id="task-1", job_id="job-1", role=AgentRole.BACKEND_ENGINEER, service="api", status=TaskStatus.IN_PROGRESS, **kw):
    return Task(id=task_id, job_id=job_id, title="Test", description="desc", service=service, agent_role=role, status=status, **kw)


def _make_agent(agent_id="agent-1", job_id="job-1", task_id="task-1", role=AgentRole.BACKEND_ENGINEER, status="done", error=None):
    a = Agent(id=agent_id, job_id=job_id, task_id=task_id, role=role, model="test", status=status)
    a.error = error
    return a


def _mock_engine(db):
    """Build a minimal mock engine wired to a real in-memory DB."""
    engine = MagicMock()
    engine.db = db
    engine.config = MagicMock()
    engine.config.model = "test-model"
    engine.config.arbiter_enabled = False
    engine.config.max_revisions = 3
    # Real numbers, not MagicMocks: launch_spec_analyst compares these against
    # int/float, and a MagicMock silently satisfies attribute access while
    # raising on comparison.
    engine.config.max_jobs_per_hour = 1000
    engine.config.max_jobs_per_month = 10000
    # Real numbers: the reviewer fan-out compares these against int/float, and a
    # MagicMock satisfies attribute access while raising on comparison.
    engine.config.job_cost_limit_usd = 1000.0
    engine.config.agent_cost_limit_usd = 100.0
    engine.config.require_ci_pass = False
    engine.config.model_reviewer = "test-reviewer-model"
    engine.config.classifier_enabled = False
    engine.config.model_easy = "test-easy"
    engine.config.model_medium = "test-medium"
    engine.config.model_hard = "test-hard"
    engine.registry = {}
    engine._k8s_enabled = False
    engine._has_running_agent = AsyncMock(return_value=False)
    engine._run_in_process = AsyncMock()
    engine._nats_agent_status = AsyncMock()
    engine._trello_comment = AsyncMock()
    engine._spawn = MagicMock()
    engine._maybe_dry_run = MagicMock(side_effect=lambda x: x)
    engine._on_job_terminal = AsyncMock()
    return engine


# ---------------------------------------------------------------------------
# Spec analyst — task marked done when agent succeeds
# ---------------------------------------------------------------------------


class TestSpecAnalystCompletion:
    async def test_task_marked_done_on_success(self, db):
        """When spec analyst agent finishes 'done', its task should be marked DONE."""
        from minions.engine.dev import launch_spec_analyst

        job = await db.create_job("test spec")
        engine = _mock_engine(db)
        done_agent = _make_agent(role=AgentRole.SPEC_ANALYST, status="done")
        engine._run_in_process.return_value = done_agent

        await launch_spec_analyst(engine, job)

        # Find the spec analyst task
        tasks = await db.get_tasks(job.id)
        spec_tasks = [t for t in tasks if t.agent_role == AgentRole.SPEC_ANALYST]
        assert len(spec_tasks) == 1
        assert spec_tasks[0].status == TaskStatus.DONE

    async def test_task_marked_failed_on_failure(self, db):
        """When spec analyst agent fails, task should be marked FAILED."""
        from minions.engine.dev import launch_spec_analyst

        job = await db.create_job("test spec")
        engine = _mock_engine(db)
        failed_agent = _make_agent(role=AgentRole.SPEC_ANALYST, status="failed", error="timeout")
        engine._run_in_process.return_value = failed_agent

        await launch_spec_analyst(engine, job)

        tasks = await db.get_tasks(job.id)
        spec_tasks = [t for t in tasks if t.agent_role == AgentRole.SPEC_ANALYST]
        assert len(spec_tasks) == 1
        assert spec_tasks[0].status == TaskStatus.FAILED


# ---------------------------------------------------------------------------
# Arbiter — task marked done when agent succeeds
# ---------------------------------------------------------------------------


class TestArbiterCompletion:
    async def test_task_marked_done_on_success(self, db):
        """When arbiter agent finishes 'done', its task should be marked DONE."""
        from minions.engine.dev import launch_arbiter

        job = await db.create_job("test spec")
        await db.update_job_status(job.id, JobStatus.SPEC_READY)
        engine = _mock_engine(db)
        done_agent = _make_agent(role=AgentRole.ARBITER, status="done")
        engine._run_in_process.return_value = done_agent

        await launch_arbiter(engine, job)

        tasks = await db.get_tasks(job.id)
        arbiter_tasks = [t for t in tasks if t.agent_role == AgentRole.ARBITER]
        assert len(arbiter_tasks) == 1
        assert arbiter_tasks[0].status == TaskStatus.DONE

    async def test_task_marked_failed_on_failure(self, db):
        """When arbiter agent fails, task should be marked FAILED."""
        from minions.engine.dev import launch_arbiter

        job = await db.create_job("test spec")
        await db.update_job_status(job.id, JobStatus.SPEC_READY)
        engine = _mock_engine(db)
        failed_agent = _make_agent(role=AgentRole.ARBITER, status="failed", error="crash")
        engine._run_in_process.return_value = failed_agent

        await launch_arbiter(engine, job)

        tasks = await db.get_tasks(job.id)
        arbiter_tasks = [t for t in tasks if t.agent_role == AgentRole.ARBITER]
        assert len(arbiter_tasks) == 1
        assert arbiter_tasks[0].status == TaskStatus.FAILED


# ---------------------------------------------------------------------------
# Engineer — task marked done when agent succeeds (non-revision)
# ---------------------------------------------------------------------------


class TestEngineerCompletion:
    async def test_task_pr_open_on_success_with_pr(self, db, sample_job, make_task):
        """When engineer finishes with a PR, task should go to PR_OPEN for review."""
        from minions.engine.dev import run_engineer

        job = sample_job
        task = make_task(job.id)
        task = await db.create_task(task)
        engine = _mock_engine(db)
        engine._resolve_service.return_value = (MagicMock(), MagicMock(repo_path="/tmp"))

        # Simulate agent creating a PR during execution
        async def fake_run(job, task, agent, project, service, context=None, **kwargs):
            await db.update_task(task.id, pr_url="https://gitlab.com/mr/1", pr_number=1, branch_name="feat/test")
            return _make_agent(status="done")

        engine._run_in_process.side_effect = fake_run

        await run_engineer(engine, job, task)

        updated = await db.get_task(task.id)
        assert updated.status == TaskStatus.PR_OPEN

    async def test_task_retried_on_success_without_pr(self, db, sample_job, make_task):
        """When engineer finishes without creating a PR, task should be retried."""
        from minions.engine.dev import run_engineer

        job = sample_job
        task = make_task(job.id)
        task = await db.create_task(task)
        engine = _mock_engine(db)
        engine._resolve_service.return_value = (MagicMock(), MagicMock(repo_path="/tmp"))
        done_agent = _make_agent(status="done")
        engine._run_in_process.return_value = done_agent

        await run_engineer(engine, job, task)

        updated = await db.get_task(task.id)
        assert updated.status == TaskStatus.PENDING
        assert updated.attempt == 2

    async def test_task_marked_failed_on_failure(self, db, sample_job, make_task):
        """When engineer agent fails, task should be marked FAILED."""
        from minions.engine.dev import run_engineer

        job = sample_job
        task = make_task(job.id)
        task = await db.create_task(task)
        engine = _mock_engine(db)
        engine._resolve_service.return_value = (MagicMock(), MagicMock(repo_path="/tmp"))
        failed_agent = _make_agent(status="failed", error="out of turns")
        engine._run_in_process.return_value = failed_agent

        await run_engineer(engine, job, task)

        updated = await db.get_task(task.id)
        assert updated.status == TaskStatus.FAILED

    async def test_task_already_pr_open_not_overwritten(self, db, sample_job, make_task):
        """If agent transitions task to PR_OPEN itself, engine should not error."""
        from minions.engine.dev import run_engineer

        job = sample_job
        task = make_task(job.id)
        task = await db.create_task(task)
        engine = _mock_engine(db)
        engine._resolve_service.return_value = (MagicMock(), MagicMock(repo_path="/tmp"))

        # Simulate agent moving task to PR_OPEN during execution
        async def fake_run(job, task, agent, project, service, context=None, **kwargs):
            await db.update_task(task.id, pr_url="https://gitlab.com/mr/1", pr_number=1, branch_name="feat/test")
            await db.update_task(task.id, status=TaskStatus.PR_OPEN, agent_role="")
            return _make_agent(status="done")

        engine._run_in_process.side_effect = fake_run

        await run_engineer(engine, job, task)

        updated = await db.get_task(task.id)
        assert updated.status == TaskStatus.PR_OPEN

    async def test_unknown_service_fails_task(self, db, sample_job, make_task):
        """Task with unresolvable service is marked FAILED."""
        from minions.engine.dev import run_engineer

        job = sample_job
        task = make_task(job.id, service="nonexistent")
        task = await db.create_task(task)
        engine = _mock_engine(db)
        engine._resolve_service.return_value = (None, None)

        await run_engineer(engine, job, task)

        updated = await db.get_task(task.id)
        assert updated.status == TaskStatus.FAILED
        assert "Unknown service" in updated.error


# ---------------------------------------------------------------------------
# launch_engineers — sequential per-service, gates on arbiter
# ---------------------------------------------------------------------------


class TestLaunchEngineers:
    async def test_gates_on_arbiter_running(self, db, sample_job):
        """Should not launch engineers while arbiter is still running."""
        from minions.engine.dev import launch_engineers

        engine = _mock_engine(db)
        engine._has_running_agent.return_value = True

        await launch_engineers(engine, sample_job)

        engine._spawn.assert_not_called()

    async def test_sequential_per_service(self, db, sample_job, make_task):
        """Only one task per service should be launched at a time."""
        from minions.engine.dev import launch_engineers

        job = sample_job
        await db.update_job_status(job.id, JobStatus.SPEC_READY)
        await db.update_job_status(job.id, JobStatus.TASKS_CREATED)

        # Create 3 pending tasks for the same service
        t1 = make_task(job.id, title="Task 1")
        t2 = make_task(job.id, title="Task 2")
        t3 = make_task(job.id, title="Task 3")
        await db.create_task(t1)
        await db.create_task(t2)
        await db.create_task(t3)

        engine = _mock_engine(db)
        await launch_engineers(engine, job)

        # Only one task should be spawned (first pending for "api" service)
        assert engine._spawn.call_count == 1

    async def test_parallel_across_services(self, db, sample_job, make_task):
        """Tasks targeting different services can launch in parallel."""
        from minions.engine.dev import launch_engineers

        job = sample_job
        await db.update_job_status(job.id, JobStatus.SPEC_READY)
        await db.update_job_status(job.id, JobStatus.TASKS_CREATED)

        t1 = make_task(job.id, title="Task 1", service="api")
        t2 = make_task(job.id, title="Task 2", service="frontend")
        await db.create_task(t1)
        await db.create_task(t2)

        engine = _mock_engine(db)
        await launch_engineers(engine, job)

        # Both should be spawned (different services)
        assert engine._spawn.call_count == 2

    async def test_skips_busy_service(self, db, sample_job, make_task):
        """Pending task for a service with an in-progress task should wait."""
        from minions.engine.dev import launch_engineers

        job = sample_job
        await db.update_job_status(job.id, JobStatus.SPEC_READY)
        await db.update_job_status(job.id, JobStatus.TASKS_CREATED)

        # One in-progress, one pending for same service
        t1 = make_task(job.id, title="Running", service="api")
        await db.create_task(t1)
        await db.update_task(t1.id, status="in_progress")

        t2 = make_task(job.id, title="Waiting", service="api")
        await db.create_task(t2)

        engine = _mock_engine(db)
        await launch_engineers(engine, job)

        # t2 should NOT be spawned — api service is busy
        engine._spawn.assert_not_called()

    async def test_skips_service_in_review(self, db, sample_job, make_task):
        """Pending task should wait when another task for same service is in PR_OPEN or IN_REVIEW."""
        from minions.engine.dev import launch_engineers

        job = sample_job
        await db.update_job_status(job.id, JobStatus.SPEC_READY)
        await db.update_job_status(job.id, JobStatus.TASKS_CREATED)

        # Task 1 is in review cycle (PR_OPEN), task 2 is pending — same service
        t1 = make_task(job.id, title="In review", service="api")
        await db.create_task(t1)
        await db.update_task(t1.id, status=TaskStatus.IN_PROGRESS)
        await db.update_task(t1.id, pr_url="https://gitlab.com/mr/1", pr_number=1, branch_name="feat/test")
        await db.update_task(t1.id, status=TaskStatus.PR_OPEN, agent_role="")

        t2 = make_task(job.id, title="Waiting", service="api")
        await db.create_task(t2)

        engine = _mock_engine(db)
        await launch_engineers(engine, job)

        # t2 should NOT be spawned — api service has a task in PR_OPEN
        engine._spawn.assert_not_called()

    async def test_skips_service_in_review_manage_dev_tasks(self, db, sample_job, make_task):
        """manage_dev_tasks should not launch pending task when same service has IN_REVIEW task."""
        from minions.engine.dev import manage_dev_tasks

        job = sample_job
        await db.update_job_status(job.id, JobStatus.SPEC_READY)
        await db.update_job_status(job.id, JobStatus.TASKS_CREATED)
        await db.update_job_status(job.id, JobStatus.DEV_IN_PROGRESS)

        # Task 1 is in IN_REVIEW, task 2 is pending — same service
        t1 = make_task(job.id, title="In review", service="api")
        await db.create_task(t1)
        await db.update_task(t1.id, status=TaskStatus.IN_PROGRESS)
        await db.update_task(t1.id, pr_url="https://gitlab.com/mr/1", pr_number=1, branch_name="feat/test")
        await db.update_task(t1.id, status=TaskStatus.PR_OPEN, agent_role="")
        await db.update_task(t1.id, status=TaskStatus.IN_REVIEW, agent_role="")

        # Create a running reviewer agent so stuck-reviewer recovery doesn't kick in
        agent = Agent(job_id=job.id, role=AgentRole.CODE_REVIEWER, task_id=t1.id, model="test", status="running")
        await db.create_agent(agent)
        await db.update_agent(agent.id, status="running")

        t2 = make_task(job.id, title="Waiting", service="api")
        await db.create_task(t2)

        engine = _mock_engine(db)
        await manage_dev_tasks(engine, job)

        # t2 should NOT be spawned — api service has a task in IN_REVIEW
        engine._spawn.assert_not_called()

    async def test_launches_after_service_freed(self, db, sample_job, make_task):
        """Pending task should launch once the prior task for that service is terminal."""
        from minions.engine.dev import launch_engineers

        job = sample_job
        await db.update_job_status(job.id, JobStatus.SPEC_READY)
        await db.update_job_status(job.id, JobStatus.TASKS_CREATED)

        # Task 1 is merged (terminal), task 2 is pending — same service
        t1 = make_task(job.id, title="Merged", service="api")
        await db.create_task(t1)
        await db.update_task(t1.id, status=TaskStatus.IN_PROGRESS)
        await db.update_task(t1.id, pr_url="https://gitlab.com/mr/1", pr_number=1, branch_name="feat/test")
        await db.update_task(t1.id, status=TaskStatus.PR_OPEN, agent_role="")
        await db.update_task(t1.id, status=TaskStatus.IN_REVIEW, agent_role="")
        await db.update_task(t1.id, status=TaskStatus.MERGED, agent_role="code_reviewer")

        t2 = make_task(job.id, title="Ready to go", service="api")
        await db.create_task(t2)

        engine = _mock_engine(db)
        await launch_engineers(engine, job)

        # t2 should be spawned — api service is free (t1 is terminal)
        assert engine._spawn.call_count == 1


# ---------------------------------------------------------------------------
# manage_dev_tasks — orphan recovery
# ---------------------------------------------------------------------------


class TestOrphanRecovery:
    async def test_done_agent_with_pr_goes_to_pr_open(self, db, sample_job, make_task):
        """Orphaned engineer task with done agent and PR should go to PR_OPEN."""
        from minions.engine.dev import manage_dev_tasks

        job = sample_job
        await db.update_job_status(job.id, JobStatus.SPEC_READY)
        await db.update_job_status(job.id, JobStatus.TASKS_CREATED)
        await db.update_job_status(job.id, JobStatus.DEV_IN_PROGRESS)

        task = make_task(job.id)
        task = await db.create_task(task)
        await db.update_task(task.id, status="in_progress")
        await db.update_task(task.id, pr_url="https://gitlab.com/mr/1", pr_number=1, branch_name="feat/test")

        agent = Agent(job_id=job.id, role=AgentRole.BACKEND_ENGINEER, task_id=task.id, model="test", status="done")
        await db.create_agent(agent)
        await db.update_agent(agent.id, status="done")

        engine = _mock_engine(db)
        await manage_dev_tasks(engine, job)

        updated = await db.get_task(task.id)
        assert updated.status == TaskStatus.PR_OPEN

    async def test_done_agent_without_pr_retries(self, db, sample_job, make_task):
        """Orphaned engineer task with done agent but no PR should retry."""
        from minions.engine.dev import manage_dev_tasks

        job = sample_job
        await db.update_job_status(job.id, JobStatus.SPEC_READY)
        await db.update_job_status(job.id, JobStatus.TASKS_CREATED)
        await db.update_job_status(job.id, JobStatus.DEV_IN_PROGRESS)

        task = make_task(job.id)
        task = await db.create_task(task)
        await db.update_task(task.id, status="in_progress")

        agent = Agent(job_id=job.id, role=AgentRole.BACKEND_ENGINEER, task_id=task.id, model="test", status="done")
        await db.create_agent(agent)
        await db.update_agent(agent.id, status="done")

        engine = _mock_engine(db)
        await manage_dev_tasks(engine, job)

        updated = await db.get_task(task.id)
        assert updated.status == TaskStatus.PENDING
        assert updated.attempt == 2

    async def test_failed_agent_retries_task(self, db, sample_job, make_task):
        """Orphaned task with a failed agent should be retried if attempts remain."""
        from minions.engine.dev import manage_dev_tasks

        job = sample_job
        await db.update_job_status(job.id, JobStatus.SPEC_READY)
        await db.update_job_status(job.id, JobStatus.TASKS_CREATED)
        await db.update_job_status(job.id, JobStatus.DEV_IN_PROGRESS)

        task = make_task(job.id)
        task = await db.create_task(task)
        await db.update_task(task.id, status="in_progress")

        agent = Agent(job_id=job.id, role=AgentRole.BACKEND_ENGINEER, task_id=task.id, model="test", status="failed")
        await db.create_agent(agent)
        await db.update_agent(agent.id, status="failed")

        engine = _mock_engine(db)
        await manage_dev_tasks(engine, job)

        updated = await db.get_task(task.id)
        assert updated.status == TaskStatus.PENDING
        assert updated.attempt == 2

    async def test_failed_agent_max_attempts_fails_task(self, db, sample_job, make_task):
        """Orphaned task at max attempts with failed agent should be marked FAILED."""
        from minions.engine.dev import manage_dev_tasks

        job = sample_job
        await db.update_job_status(job.id, JobStatus.SPEC_READY)
        await db.update_job_status(job.id, JobStatus.TASKS_CREATED)
        await db.update_job_status(job.id, JobStatus.DEV_IN_PROGRESS)

        task = make_task(job.id)
        task = await db.create_task(task)
        await db.update_task(task.id, status="in_progress")
        # Set attempt to max
        await db.update_task(task.id, attempt=3)

        agent = Agent(job_id=job.id, role=AgentRole.BACKEND_ENGINEER, task_id=task.id, model="test", status="failed")
        await db.create_agent(agent)
        await db.update_agent(agent.id, status="failed")

        engine = _mock_engine(db)
        await manage_dev_tasks(engine, job)

        updated = await db.get_task(task.id)
        assert updated.status == TaskStatus.FAILED


# ---------------------------------------------------------------------------
# manage_dev_tasks — all terminal detection
# ---------------------------------------------------------------------------


class TestJobCompletion:
    async def test_all_done_advances_to_merged(self, db, sample_job, make_task):
        """When all dev tasks are DONE, job should advance to MERGED."""
        from minions.engine.dev import manage_dev_tasks

        job = sample_job
        await db.update_job_status(job.id, JobStatus.SPEC_READY)
        await db.update_job_status(job.id, JobStatus.TASKS_CREATED)
        await db.update_job_status(job.id, JobStatus.DEV_IN_PROGRESS)

        task = make_task(job.id)
        task = await db.create_task(task)
        await db.update_task(task.id, status="in_progress")
        await db.update_task(task.id, status="done", agent_role="")

        engine = _mock_engine(db)
        await manage_dev_tasks(engine, job)

        updated_job = await db.get_job(job.id)
        assert updated_job.status == JobStatus.MERGED

    async def test_all_failed_marks_job_failed(self, db, sample_job, make_task):
        """When all dev tasks are FAILED, job should be marked FAILED."""
        from minions.engine.dev import manage_dev_tasks

        job = sample_job
        await db.update_job_status(job.id, JobStatus.SPEC_READY)
        await db.update_job_status(job.id, JobStatus.TASKS_CREATED)
        await db.update_job_status(job.id, JobStatus.DEV_IN_PROGRESS)

        task = make_task(job.id)
        task = await db.create_task(task)
        await db.update_task(task.id, status="in_progress")
        await db.update_task(task.id, status="failed", agent_role="", error="boom")

        engine = _mock_engine(db)
        engine._on_job_terminal = AsyncMock()
        await manage_dev_tasks(engine, job)

        updated_job = await db.get_job(job.id)
        assert updated_job.status == JobStatus.FAILED

    async def test_pending_tasks_picked_up_after_completion(self, db, sample_job, make_task):
        """After one task completes, the next pending task for that service should be spawned."""
        from minions.engine.dev import manage_dev_tasks

        job = sample_job
        await db.update_job_status(job.id, JobStatus.SPEC_READY)
        await db.update_job_status(job.id, JobStatus.TASKS_CREATED)
        await db.update_job_status(job.id, JobStatus.DEV_IN_PROGRESS)

        # Task 1 done, Task 2 pending — same service
        t1 = make_task(job.id, title="Done task")
        t1 = await db.create_task(t1)
        await db.update_task(t1.id, status="in_progress")
        await db.update_task(t1.id, status="done", agent_role="")

        t2 = make_task(job.id, title="Pending task")
        await db.create_task(t2)

        engine = _mock_engine(db)
        await manage_dev_tasks(engine, job)

        # t2 should be spawned since t1 is done and api service is free
        assert engine._spawn.call_count == 1


# ---------------------------------------------------------------------------
# Subtask gating — task cannot be DONE with incomplete subtasks
# ---------------------------------------------------------------------------


async def _make_subtasks(db, task_id, statuses):
    """Create subtasks with the given statuses."""
    for i, status in enumerate(statuses, 1):
        st = Subtask(task_id=task_id, sequence_num=i, description=f"Step {i}")
        st = await db.create_subtask(st)
        if status == "running":
            await db.update_subtask(st.id, status="running")
        elif status == "completed":
            await db.update_subtask(st.id, status="running")
            await db.update_subtask(st.id, status="completed")
        elif status == "failed":
            await db.update_subtask(st.id, status="running")
            await db.update_subtask(st.id, status="failed")


class TestSubtaskGating:
    async def test_task_pr_open_when_all_subtasks_completed_with_pr(self, db, sample_job, make_task):
        """Engineer task goes to PR_OPEN if all subtasks completed and PR exists."""
        from minions.engine.dev import run_engineer

        job = sample_job
        task = make_task(job.id)
        task = await db.create_task(task)
        await _make_subtasks(db, task.id, ["completed", "completed", "completed"])

        engine = _mock_engine(db)
        engine._resolve_service.return_value = (MagicMock(), MagicMock(repo_path="/tmp"))

        async def fake_run(job, task, agent, project, service, context=None, **kwargs):
            await db.update_task(task.id, pr_url="https://gitlab.com/mr/1", pr_number=1, branch_name="feat/test")
            return _make_agent(status="done")

        engine._run_in_process.side_effect = fake_run

        await run_engineer(engine, job, task)

        updated = await db.get_task(task.id)
        assert updated.status == TaskStatus.PR_OPEN

    async def test_db_engineer_task_done_when_all_subtasks_completed(self, db, sample_job, make_task):
        """Database engineer task (no PR needed) goes to DONE directly."""
        from minions.engine.dev import run_engineer

        job = sample_job
        task = make_task(job.id, agent_role=AgentRole.DATABASE_ENGINEER)
        task = await db.create_task(task)
        await _make_subtasks(db, task.id, ["completed", "completed"])

        engine = _mock_engine(db)
        engine._resolve_service.return_value = (MagicMock(), MagicMock(repo_path="/tmp"))
        engine._run_in_process.return_value = _make_agent(status="done")

        await run_engineer(engine, job, task)

        updated = await db.get_task(task.id)
        assert updated.status == TaskStatus.DONE

    async def test_task_not_done_with_pending_subtasks(self, db, sample_job, make_task):
        """Task should NOT be marked DONE if subtasks are still pending — retries instead."""
        from minions.engine.dev import run_engineer

        job = sample_job
        task = make_task(job.id)
        task = await db.create_task(task)
        await _make_subtasks(db, task.id, ["completed", "running", "pending"])

        engine = _mock_engine(db)
        engine._resolve_service.return_value = (MagicMock(), MagicMock(repo_path="/tmp"))
        engine._run_in_process.return_value = _make_agent(status="done")

        await run_engineer(engine, job, task)

        updated = await db.get_task(task.id)
        # Should be retried (pending), not done
        assert updated.status == TaskStatus.PENDING
        assert updated.attempt == 2

    async def test_task_fails_with_incomplete_subtasks_max_attempts(self, db, sample_job, make_task):
        """Task FAILED if subtasks incomplete and no retries left."""
        from minions.engine.dev import run_engineer

        job = sample_job
        task = make_task(job.id)
        task = await db.create_task(task)
        await _make_subtasks(db, task.id, ["completed", "pending"])

        engine = _mock_engine(db)
        engine._resolve_service.return_value = (MagicMock(), MagicMock(repo_path="/tmp"))
        engine._run_in_process.return_value = _make_agent(status="done")

        # Set to max attempts before running
        await db.update_task(task.id, attempt=3)

        await run_engineer(engine, job, task)

        updated = await db.get_task(task.id)
        assert updated.status == TaskStatus.FAILED
        assert "incomplete subtasks" in updated.error

    async def test_task_retried_with_no_subtasks_and_no_pr(self, db, sample_job, make_task):
        """Engineer task with no subtasks but no PR should retry."""
        from minions.engine.dev import run_engineer

        job = sample_job
        task = make_task(job.id)
        task = await db.create_task(task)

        engine = _mock_engine(db)
        engine._resolve_service.return_value = (MagicMock(), MagicMock(repo_path="/tmp"))
        engine._run_in_process.return_value = _make_agent(status="done")

        await run_engineer(engine, job, task)

        updated = await db.get_task(task.id)
        assert updated.status == TaskStatus.PENDING
        assert updated.attempt == 2

    async def test_task_pr_open_with_mixed_terminal_subtasks(self, db, sample_job, make_task):
        """Engineer task with PR goes to PR_OPEN even with mix of completed/failed subtasks."""
        from minions.engine.dev import run_engineer

        job = sample_job
        task = make_task(job.id)
        task = await db.create_task(task)
        await _make_subtasks(db, task.id, ["completed", "failed", "completed"])

        engine = _mock_engine(db)
        engine._resolve_service.return_value = (MagicMock(), MagicMock(repo_path="/tmp"))

        async def fake_run(job, task, agent, project, service, context=None, **kwargs):
            await db.update_task(task.id, pr_url="https://gitlab.com/mr/1", pr_number=1, branch_name="feat/test")
            return _make_agent(status="done")

        engine._run_in_process.side_effect = fake_run

        await run_engineer(engine, job, task)

        updated = await db.get_task(task.id)
        assert updated.status == TaskStatus.PR_OPEN

    async def test_orphan_recovery_checks_subtasks(self, db, sample_job, make_task):
        """Orphan recovery should also check subtask completion before marking done."""
        from minions.engine.dev import manage_dev_tasks

        job = sample_job
        await db.update_job_status(job.id, JobStatus.SPEC_READY)
        await db.update_job_status(job.id, JobStatus.TASKS_CREATED)
        await db.update_job_status(job.id, JobStatus.DEV_IN_PROGRESS)

        task = make_task(job.id)
        task = await db.create_task(task)
        await db.update_task(task.id, status="in_progress")
        await _make_subtasks(db, task.id, ["completed", "running"])

        # Agent is done but subtasks aren't
        agent = Agent(job_id=job.id, role=AgentRole.BACKEND_ENGINEER, task_id=task.id, model="test", status="done")
        await db.create_agent(agent)
        await db.update_agent(agent.id, status="done")

        engine = _mock_engine(db)
        await manage_dev_tasks(engine, job)

        updated = await db.get_task(task.id)
        # Should retry, not mark done
        assert updated.status == TaskStatus.PENDING
        assert updated.attempt == 2


# ---------------------------------------------------------------------------
# Revision completion — race condition: task already advanced to IN_REVIEW
# ---------------------------------------------------------------------------


class TestRevisionCompletionRace:
    """Regression tests for the race where the poll loop advances a task to
    IN_REVIEW before the revision engineer's completion handler runs."""

    async def _setup_in_progress_task(self, db, job, make_task):
        """Helper: create a task and walk it to IN_PROGRESS with PR metadata."""
        task = make_task(job.id)
        task = await db.create_task(task)
        await db.update_task(task.id, status=TaskStatus.IN_PROGRESS)
        await db.update_task(task.id, pr_url="https://gitlab.com/mr/1", pr_number=1, branch_name="feat/test")
        return task

    async def test_revision_does_not_clobber_in_review(self, db, sample_job, make_task):
        """If the poll loop already moved the task to IN_REVIEW, the revision
        completion handler must NOT reset it back to PR_OPEN."""
        from minions.engine.dev import run_engineer

        job = sample_job
        task = await self._setup_in_progress_task(db, job, make_task)
        engine = _mock_engine(db)
        engine._resolve_service.return_value = (MagicMock(), MagicMock(repo_path="/tmp"))

        # Simulate: agent tool call sets PR_OPEN, then poll loop advances to IN_REVIEW
        # before run_agent() returns.
        async def fake_run(job, task, agent, project, service, context=None, **kwargs):
            await db.update_task(task.id, status=TaskStatus.PR_OPEN, agent_role="")
            await db.update_task(task.id, status=TaskStatus.IN_REVIEW, agent_role="")
            return _make_agent(status="done")

        engine._run_in_process.side_effect = fake_run

        await run_engineer(engine, job, task, is_revision=True)

        updated = await db.get_task(task.id)
        # Must stay in IN_REVIEW, NOT regress to PR_OPEN
        assert updated.status == TaskStatus.IN_REVIEW

    async def test_revision_does_not_clobber_pr_open(self, db, sample_job, make_task):
        """If the task is already PR_OPEN (agent set it during execution),
        the completion handler should not try to set it again."""
        from minions.engine.dev import run_engineer

        job = sample_job
        task = await self._setup_in_progress_task(db, job, make_task)
        engine = _mock_engine(db)
        engine._resolve_service.return_value = (MagicMock(), MagicMock(repo_path="/tmp"))

        async def fake_run(job, task, agent, project, service, context=None, **kwargs):
            await db.update_task(task.id, status=TaskStatus.PR_OPEN, agent_role="")
            return _make_agent(status="done")

        engine._run_in_process.side_effect = fake_run

        await run_engineer(engine, job, task, is_revision=True)

        updated = await db.get_task(task.id)
        assert updated.status == TaskStatus.PR_OPEN

    async def test_revision_sets_pr_open_when_still_in_progress(self, db, sample_job, make_task):
        """Normal case: task is still IN_PROGRESS when agent finishes — should advance to PR_OPEN."""
        from minions.engine.dev import run_engineer

        job = sample_job
        task = await self._setup_in_progress_task(db, job, make_task)
        engine = _mock_engine(db)
        engine._resolve_service.return_value = (MagicMock(), MagicMock(repo_path="/tmp"))
        engine._run_in_process.return_value = _make_agent(status="done")

        await run_engineer(engine, job, task, is_revision=True)

        updated = await db.get_task(task.id)
        assert updated.status == TaskStatus.PR_OPEN


# ---------------------------------------------------------------------------
# run_task_review — single agent creation (no duplicates)
# ---------------------------------------------------------------------------


class TestReviewAgentCreation:
    """Regression tests for double-agent creation in run_task_review.

    Originally "exactly one agent". Now one per specialist — the fan-out creates
    N reviewer tasks. The property being protected is unchanged: each reviewer
    gets exactly ONE agent, and run_agent receives it rather than creating a
    second internally.
    """

    async def test_creates_one_agent_per_specialist(self, db, sample_job, make_task):
        """One agent per reviewer task — never two for the same reviewer."""
        from unittest.mock import patch

        from minions.engine.dev import run_task_review

        job = sample_job
        task = make_task(job.id)
        task = await db.create_task(task)
        await db.update_task(task.id, status=TaskStatus.IN_PROGRESS)
        await db.update_task(task.id, pr_url="https://gitlab.com/mr/1", mr_id="1")

        engine = _mock_engine(db)
        mock_project = MagicMock()
        mock_project.model = "claude-sonnet-4-6"
        mock_project.project_id = "test/repo"
        mock_project.auto_merge = False
        mock_service = MagicMock(repo_path="/tmp")
        engine._resolve_service.return_value = (mock_project, mock_service)

        # Mock run_agent to capture calls and return a done agent
        mock_result = _make_agent(role=AgentRole.CODE_REVIEWER, status="done")
        mock_result._review_verdict = "approve"
        mock_result._review_comments_posted = 0

        with patch("minions.engine.dev.run_agent", new_callable=AsyncMock, return_value=mock_result) as mock_run:
            with patch("minions.engine.review._create_provider_for_project") as mock_provider_factory:
                mock_provider = AsyncMock()
                mock_provider.get_changed_files.return_value = ["file.py"]
                mock_provider_factory.return_value = mock_provider

                await run_task_review(engine, job, task)

                # Every reviewer must receive its pre-created agent.
                assert mock_run.call_count >= 1, "the fan-out ran no reviewers at all"
                for call in mock_run.call_args_list:
                    assert call.kwargs.get("agent") is not None, "run_agent must receive agent= to prevent double creation"

        # One agent per reviewer task — the double-creation regression would
        # show up as more agents than tasks.
        agents = await db.get_agents_for_job(job.id)
        reviewer_agents = [a for a in agents if a.role == AgentRole.CODE_REVIEWER]
        reviewer_tasks = [t for t in await db.get_tasks(job.id) if t.agent_role == AgentRole.CODE_REVIEWER]

        assert len(reviewer_agents) == len(reviewer_tasks) == mock_run.call_count
        assert len({t.specialty for t in reviewer_tasks}) == len(reviewer_tasks), "each specialist must be distinct"

    async def test_agent_uses_project_model(self, db, sample_job, make_task):
        """The reviewer agent should use the project model, not engine.config.model."""
        from unittest.mock import patch

        from minions.engine.dev import run_task_review

        job = sample_job
        task = make_task(job.id)
        task = await db.create_task(task)
        await db.update_task(task.id, status=TaskStatus.IN_PROGRESS)
        await db.update_task(task.id, pr_url="https://gitlab.com/mr/1", mr_id="1")

        engine = _mock_engine(db)
        engine.config.model = "claude-opus-4-6"
        mock_project = MagicMock()
        mock_project.model = "claude-sonnet-4-6"
        mock_project.project_id = "test/repo"
        mock_project.auto_merge = False
        mock_service = MagicMock(repo_path="/tmp")
        engine._resolve_service.return_value = (mock_project, mock_service)

        mock_result = _make_agent(role=AgentRole.CODE_REVIEWER, status="done")
        mock_result._review_verdict = "approve"
        mock_result._review_comments_posted = 0

        with patch("minions.engine.dev.run_agent", new_callable=AsyncMock, return_value=mock_result) as mock_run:
            with patch("minions.engine.review._create_provider_for_project") as mock_provider_factory:
                mock_provider = AsyncMock()
                mock_provider.get_changed_files.return_value = []
                mock_provider_factory.return_value = mock_provider

                await run_task_review(engine, job, task)

        # Every reviewer agent should use the project model, not the engine
        # default and not the reviewer tier — an explicitly pinned project model
        # outranks both.
        agents = await db.get_agents_for_job(job.id)
        reviewer_agents = [a for a in agents if a.role == AgentRole.CODE_REVIEWER]
        assert reviewer_agents, "no reviewer agents were created"
        assert reviewer_agents[0].model == "claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# manage_dev_tasks — PR_OPEN spawns exactly one reviewer
# ---------------------------------------------------------------------------


class TestManageDevTasksReviewSpawn:
    async def _make_pr_open_task(self, db, job, make_task):
        """Helper: create a task and walk it to PR_OPEN with all required fields."""
        task = make_task(job.id)
        task = await db.create_task(task)
        await db.update_task(task.id, status=TaskStatus.IN_PROGRESS)
        await db.update_task(task.id, pr_url="https://gitlab.com/mr/1", pr_number=1, branch_name="feat/test")
        await db.update_task(task.id, status=TaskStatus.PR_OPEN, agent_role="")
        return task

    async def test_pr_open_spawns_single_reviewer(self, db, sample_job, make_task):
        """When a task reaches PR_OPEN, manage_dev_tasks should spawn exactly one reviewer."""
        from minions.engine.dev import manage_dev_tasks

        job = sample_job
        await db.update_job_status(job.id, JobStatus.SPEC_READY)
        await db.update_job_status(job.id, JobStatus.TASKS_CREATED)
        await db.update_job_status(job.id, JobStatus.DEV_IN_PROGRESS)

        task = await self._make_pr_open_task(db, job, make_task)

        engine = _mock_engine(db)
        await manage_dev_tasks(engine, job)

        # Should spawn exactly one reviewer
        assert engine._spawn.call_count == 1

        # Task should now be IN_REVIEW
        updated = await db.get_task(task.id)
        assert updated.status == TaskStatus.IN_REVIEW

    async def test_in_review_does_not_spawn_another_reviewer(self, db, sample_job, make_task):
        """A task already in IN_REVIEW should NOT trigger another reviewer spawn."""
        from minions.engine.dev import manage_dev_tasks

        job = sample_job
        await db.update_job_status(job.id, JobStatus.SPEC_READY)
        await db.update_job_status(job.id, JobStatus.TASKS_CREATED)
        await db.update_job_status(job.id, JobStatus.DEV_IN_PROGRESS)

        task = await self._make_pr_open_task(db, job, make_task)
        await db.update_task(task.id, status=TaskStatus.IN_REVIEW, agent_role="")

        # Create a running reviewer agent so the stuck-reviewer recovery doesn't kick in
        agent = Agent(job_id=job.id, role=AgentRole.CODE_REVIEWER, task_id=task.id, model="test", status="running")
        await db.create_agent(agent)
        await db.update_agent(agent.id, status="running")

        engine = _mock_engine(db)
        await manage_dev_tasks(engine, job)

        # No new spawns
        engine._spawn.assert_not_called()
