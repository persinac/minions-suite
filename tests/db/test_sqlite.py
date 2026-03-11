"""Tests for SQLite database CRUD operations using in-memory DB."""

import pytest

from minions.core.models import (
    Agent,
    AgentRole,
    JobStatus,
    Message,
    Subtask,
    SubtaskStatus,
    TaskStatus,
)
from minions.core.state_transitions import InvalidTransitionError, PreconditionError

# -----------------------------------------------------------------------
# Jobs
# -----------------------------------------------------------------------


class TestJobCrud:
    async def test_create_job(self, db):
        job = await db.create_job("Build a widget")
        assert job.id
        assert job.spec == "Build a widget"
        assert job.status == JobStatus.SPEC_RECEIVED

    async def test_get_job(self, db, sample_job):
        fetched = await db.get_job(sample_job.id)
        assert fetched is not None
        assert fetched.id == sample_job.id
        assert fetched.spec == sample_job.spec

    async def test_get_job_not_found(self, db):
        assert await db.get_job("nonexistent") is None

    async def test_get_all_jobs(self, db):
        await db.create_job("Job 1")
        await db.create_job("Job 2")
        jobs = await db.get_all_jobs()
        assert len(jobs) == 2

    async def test_get_active_jobs(self, db):
        j1 = await db.create_job("Active job")
        j2 = await db.create_job("Done job")
        await db.update_job_status(j2.id, JobStatus.DONE)
        active = await db.get_active_jobs()
        assert len(active) == 1
        assert active[0].id == j1.id

    async def test_update_job_status(self, db, sample_job):
        await db.update_job_status(sample_job.id, JobStatus.SPEC_READY)
        job = await db.get_job(sample_job.id)
        assert job.status == JobStatus.SPEC_READY

    async def test_update_job_status_invalid_transition(self, db, sample_job):
        with pytest.raises(InvalidTransitionError):
            await db.update_job_status(sample_job.id, JobStatus.DEPLOYED)

    async def test_update_job_spec(self, db, sample_job):
        await db.update_job_spec(sample_job.id, "Updated spec")
        job = await db.get_job(sample_job.id)
        assert job.spec == "Updated spec"

    async def test_create_job_with_external_id(self, db):
        job = await db.create_job("Test", external_id="ext-123")
        assert job.external_id == "ext-123"
        fetched = await db.get_job_by_external_id("ext-123")
        assert fetched.id == job.id

    async def test_get_job_by_external_id_not_found(self, db):
        assert await db.get_job_by_external_id("missing") is None


# -----------------------------------------------------------------------
# Review jobs
# -----------------------------------------------------------------------


class TestReviewJobCrud:
    async def test_create_review_job(self, db, sample_review_job):
        job, task = sample_review_job
        assert job.job_type == "review"
        assert job.status == JobStatus.TASKS_CREATED
        assert job.mr_url == "https://gitlab.example.com/team/api/-/merge_requests/42"
        assert task.agent_role == AgentRole.CODE_REVIEWER
        assert task.mr_url == job.mr_url
        assert task.mr_id == "42"

    async def test_review_job_task_is_persisted(self, db, sample_review_job):
        job, task = sample_review_job
        fetched = await db.get_task(task.id)
        assert fetched is not None
        assert fetched.job_id == job.id

    async def test_review_job_tasks_list(self, db, sample_review_job):
        job, task = sample_review_job
        tasks = await db.get_tasks(job.id)
        assert len(tasks) == 1
        assert tasks[0].id == task.id


# -----------------------------------------------------------------------
# Tasks
# -----------------------------------------------------------------------


class TestTaskCrud:
    async def test_create_task(self, db, sample_job, make_task):
        task = make_task(sample_job.id)
        created = await db.create_task(task)
        assert created.id == task.id
        assert created.status == TaskStatus.PENDING

    async def test_get_task(self, db, sample_job, make_task):
        task = make_task(sample_job.id)
        await db.create_task(task)
        fetched = await db.get_task(task.id)
        assert fetched is not None
        assert fetched.title == "Test task"

    async def test_get_task_not_found(self, db):
        assert await db.get_task("missing") is None

    async def test_get_tasks_for_job(self, db, sample_job, make_task):
        t1 = make_task(sample_job.id, title="Task 1")
        t2 = make_task(sample_job.id, title="Task 2")
        await db.create_task(t1)
        await db.create_task(t2)
        tasks = await db.get_tasks(sample_job.id)
        assert len(tasks) == 2

    async def test_update_task_status(self, db, sample_job, make_task):
        task = make_task(sample_job.id)
        await db.create_task(task)
        updated = await db.update_task(task.id, status="in_progress")
        assert updated.status == TaskStatus.IN_PROGRESS

    async def test_update_task_invalid_transition(self, db, sample_job, make_task):
        task = make_task(sample_job.id)
        await db.create_task(task)
        with pytest.raises(InvalidTransitionError):
            await db.update_task(task.id, status="done")

    async def test_update_task_with_pr_fields(self, db, sample_job, make_task):
        task = make_task(sample_job.id)
        await db.create_task(task)
        await db.update_task(task.id, status="in_progress")
        await db.update_task(
            task.id,
            status="pr_open",
            pr_url="https://example.com/pr/1",
            pr_number=1,
            branch_name="feat/x",
        )
        fetched = await db.get_task(task.id)
        assert fetched.status == TaskStatus.PR_OPEN
        assert fetched.pr_url == "https://example.com/pr/1"
        assert fetched.pr_number == 1
        assert fetched.branch_name == "feat/x"

    async def test_update_task_precondition_failure(self, db, sample_job, make_task):
        task = make_task(sample_job.id)
        await db.create_task(task)
        await db.update_task(task.id, status="in_progress")
        with pytest.raises((InvalidTransitionError, PreconditionError)):
            await db.update_task(task.id, status="pr_open")

    async def test_get_tasks_by_status(self, db, sample_job, make_task):
        t1 = make_task(sample_job.id, title="Task 1")
        t2 = make_task(sample_job.id, title="Task 2")
        await db.create_task(t1)
        await db.create_task(t2)
        await db.update_task(t1.id, status="in_progress")
        pending = await db.get_tasks_by_status(sample_job.id, TaskStatus.PENDING)
        assert len(pending) == 1
        assert pending[0].id == t2.id

    async def test_update_task_role_restriction(self, db, sample_job, make_task):
        """Backend engineer cannot mark task as done directly from in_progress."""
        task = make_task(sample_job.id, agent_role=AgentRole.BACKEND_ENGINEER)
        await db.create_task(task)
        await db.update_task(task.id, status="in_progress")
        with pytest.raises(InvalidTransitionError):
            await db.update_task(task.id, status="done", agent_role="backend_engineer")

    async def test_update_task_engine_bypass_with_empty_role(self, db, sample_job, make_task):
        """Engine (agent_role='') bypasses role restrictions on task transitions."""
        task = make_task(sample_job.id, agent_role=AgentRole.BACKEND_ENGINEER)
        await db.create_task(task)
        await db.update_task(task.id, status="in_progress")
        # Backend engineer can't do in_progress -> done, but engine (empty role) can
        updated = await db.update_task(task.id, status="done", agent_role="")
        assert updated.status == TaskStatus.DONE

    async def test_update_task_no_role_falls_back_to_task_role(self, db, sample_job, make_task):
        """When no agent_role is passed, the task's own role is used for validation."""
        task = make_task(sample_job.id, agent_role=AgentRole.BACKEND_ENGINEER)
        await db.create_task(task)
        await db.update_task(task.id, status="in_progress")
        # No agent_role passed → falls back to task's role (backend_engineer), which is blocked
        with pytest.raises(InvalidTransitionError):
            await db.update_task(task.id, status="done")

    async def test_update_task_engine_bypass_spec_analyst(self, db, sample_job, make_task):
        """Engine can mark spec_analyst tasks as done."""
        task = make_task(sample_job.id, agent_role=AgentRole.SPEC_ANALYST, service="_spec")
        await db.create_task(task)
        await db.update_task(task.id, status="in_progress")
        updated = await db.update_task(task.id, status="done", agent_role="")
        assert updated.status == TaskStatus.DONE

    async def test_update_task_engine_bypass_arbiter(self, db, sample_job, make_task):
        """Engine can mark arbiter tasks as done."""
        task = make_task(sample_job.id, agent_role=AgentRole.ARBITER, service="_arbiter")
        await db.create_task(task)
        await db.update_task(task.id, status="in_progress")
        updated = await db.update_task(task.id, status="done", agent_role="")
        assert updated.status == TaskStatus.DONE


# -----------------------------------------------------------------------
# Agents
# -----------------------------------------------------------------------


class TestAgentCrud:
    async def test_create_agent(self, db):
        agent = Agent(model="gpt-4o", job_id="j1", role=AgentRole.BACKEND_ENGINEER)
        created = await db.create_agent(agent)
        assert created.id == agent.id

    async def test_get_agent(self, db):
        agent = Agent(model="gpt-4o")
        await db.create_agent(agent)
        fetched = await db.get_agent(agent.id)
        assert fetched is not None
        assert fetched.model == "gpt-4o"

    async def test_get_agent_not_found(self, db):
        assert await db.get_agent("missing") is None

    async def test_update_agent(self, db):
        agent = Agent(model="gpt-4o")
        await db.create_agent(agent)
        await db.update_agent(agent.id, status="completed", cost_usd=0.05)
        fetched = await db.get_agent(agent.id)
        assert fetched.status == "completed"
        assert fetched.cost_usd == 0.05

    async def test_get_agents_for_job(self, db, sample_job):
        a1 = Agent(model="gpt-4o", job_id=sample_job.id)
        a2 = Agent(model="claude-3", job_id=sample_job.id)
        a3 = Agent(model="gpt-4o", job_id="other-job")
        await db.create_agent(a1)
        await db.create_agent(a2)
        await db.create_agent(a3)
        agents = await db.get_agents_for_job(sample_job.id)
        assert len(agents) == 2

    async def test_get_running_agents(self, db):
        a1 = Agent(model="gpt-4o", status="running")
        a2 = Agent(model="gpt-4o", status="completed")
        a3 = Agent(model="gpt-4o", status="starting")
        await db.create_agent(a1)
        await db.create_agent(a2)
        await db.create_agent(a3)
        running = await db.get_running_agents()
        assert len(running) == 2
        statuses = {a.status for a in running}
        assert statuses == {"running", "starting"}


# -----------------------------------------------------------------------
# Subtasks
# -----------------------------------------------------------------------


class TestSubtaskCrud:
    async def test_create_subtask(self, db, sample_job, make_task):
        task = make_task(sample_job.id)
        await db.create_task(task)
        subtask = Subtask(task_id=task.id, sequence_num=1, description="Step 1")
        created = await db.create_subtask(subtask)
        assert created.id == subtask.id
        assert created.status == SubtaskStatus.PENDING

    async def test_get_subtask(self, db, sample_job, make_task):
        task = make_task(sample_job.id)
        await db.create_task(task)
        subtask = Subtask(task_id=task.id, sequence_num=1, description="Step 1")
        await db.create_subtask(subtask)
        fetched = await db.get_subtask(subtask.id)
        assert fetched is not None
        assert fetched.description == "Step 1"

    async def test_get_subtasks_ordered(self, db, sample_job, make_task):
        task = make_task(sample_job.id)
        await db.create_task(task)
        s1 = Subtask(task_id=task.id, sequence_num=2, description="Second")
        s2 = Subtask(task_id=task.id, sequence_num=1, description="First")
        await db.create_subtask(s1)
        await db.create_subtask(s2)
        subtasks = await db.get_subtasks(task.id)
        assert len(subtasks) == 2
        assert subtasks[0].sequence_num == 1
        assert subtasks[1].sequence_num == 2

    async def test_update_subtask_status(self, db, sample_job, make_task):
        task = make_task(sample_job.id)
        await db.create_task(task)
        subtask = Subtask(task_id=task.id, sequence_num=1, description="Step 1")
        await db.create_subtask(subtask)
        updated = await db.update_subtask(subtask.id, status="running")
        assert updated.status == SubtaskStatus.RUNNING

    async def test_update_subtask_invalid_transition(self, db, sample_job, make_task):
        task = make_task(sample_job.id)
        await db.create_task(task)
        subtask = Subtask(task_id=task.id, sequence_num=1, description="Step 1")
        await db.create_subtask(subtask)
        with pytest.raises(InvalidTransitionError):
            await db.update_subtask(subtask.id, status="completed")

    async def test_update_subtask_with_result(self, db, sample_job, make_task):
        task = make_task(sample_job.id)
        await db.create_task(task)
        subtask = Subtask(task_id=task.id, sequence_num=1, description="Step 1")
        await db.create_subtask(subtask)
        await db.update_subtask(subtask.id, status="running")
        updated = await db.update_subtask(subtask.id, status="completed", result={"output": "done"})
        assert updated.result == {"output": "done"}

    async def test_create_subtasks_batch(self, db, sample_job, make_task):
        task = make_task(sample_job.id)
        await db.create_task(task)
        subtasks = [
            Subtask(task_id=task.id, sequence_num=i, description=f"Step {i}")
            for i in range(1, 4)
        ]
        created = await db.create_subtasks_batch(subtasks)
        assert len(created) == 3

    async def test_get_running_subtasks_all(self, db, sample_job, make_task):
        task = make_task(sample_job.id)
        await db.create_task(task)
        s1 = Subtask(task_id=task.id, sequence_num=1, description="Running")
        s2 = Subtask(task_id=task.id, sequence_num=2, description="Pending")
        await db.create_subtask(s1)
        await db.create_subtask(s2)
        await db.update_subtask(s1.id, status="running")
        running = await db.get_running_subtasks_all()
        assert len(running) == 1
        assert running[0].id == s1.id


# -----------------------------------------------------------------------
# Messages
# -----------------------------------------------------------------------


class TestMessageCrud:
    async def test_send_and_get_messages(self, db, sample_job):
        msg = Message(job_id=sample_job.id, from_role=AgentRole.ARBITER, content="Hello team")
        await db.send_message(msg)
        messages = await db.get_messages(sample_job.id)
        assert len(messages) == 1
        assert messages[0].content == "Hello team"

    async def test_get_messages_filtered_by_role(self, db, sample_job):
        m1 = Message(job_id=sample_job.id, from_role=AgentRole.ARBITER, to_role=AgentRole.BACKEND_ENGINEER, content="For you")
        m2 = Message(job_id=sample_job.id, from_role=AgentRole.ARBITER, to_role=AgentRole.FRONTEND_ENGINEER, content="Not for you")
        m3 = Message(job_id=sample_job.id, from_role=AgentRole.ARBITER, content="Broadcast")
        await db.send_message(m1)
        await db.send_message(m2)
        await db.send_message(m3)
        be_msgs = await db.get_messages(sample_job.id, role=AgentRole.BACKEND_ENGINEER)
        assert len(be_msgs) == 2  # directed + broadcast
        assert {m.content for m in be_msgs} == {"For you", "Broadcast"}


# -----------------------------------------------------------------------
# Events and audit
# -----------------------------------------------------------------------


class TestEvents:
    async def test_record_and_get_events(self, db, sample_job):
        await db.record_event(sample_job.id, "custom_event", "test", "some detail")
        events = await db.get_events(sample_job.id)
        custom = [e for e in events if e["event_type"] == "custom_event"]
        assert len(custom) == 1
        assert custom[0]["detail"] == "some detail"

    async def test_record_tool_call(self, db, sample_job):
        await db.record_tool_call(
            tool_name="submit_refined_spec",
            params={"spec": "test"},
            result="ok",
            duration_ms=42.5,
            job_id=sample_job.id,
        )
        calls = await db.get_tool_calls(sample_job.id)
        assert len(calls) == 1
        assert calls[0]["tool_name"] == "submit_refined_spec"
        assert calls[0]["duration_ms"] == 42.5

    async def test_job_timeline(self, db, sample_job):
        await db.record_tool_call("some_tool", job_id=sample_job.id)
        timeline = await db.get_job_timeline(sample_job.id)
        assert len(timeline) >= 1  # at least the job_created event + tool call

    async def test_state_transition_recorded(self, db, sample_job):
        await db.update_job_status(sample_job.id, JobStatus.SPEC_READY)
        # The transition should be recorded in the state_transitions table
        cursor = await db._db.execute("SELECT * FROM state_transitions WHERE job_id = ?", (sample_job.id,))
        rows = await cursor.fetchall()
        assert len(rows) >= 1


# -----------------------------------------------------------------------
# Heartbeats (no-op in SQLite)
# -----------------------------------------------------------------------


class TestHeartbeats:
    async def test_upsert_heartbeat_noop(self, db):
        await db.upsert_heartbeat("a1", "backend_engineer")

    async def test_get_stale_heartbeats_empty(self, db):
        result = await db.get_stale_heartbeats(300)
        assert result == []

    async def test_has_recent_heartbeat_false(self, db):
        assert await db.has_recent_heartbeat("t1", 300) is False

    async def test_delete_heartbeat_noop(self, db):
        await db.delete_heartbeat("a1")

    async def test_clear_all_heartbeats_noop(self, db):
        await db.clear_all_heartbeats()


# -----------------------------------------------------------------------
# Cost summary
# -----------------------------------------------------------------------


class TestCostSummary:
    async def test_empty_cost_summary(self, db):
        summary = await db.get_cost_summary()
        assert summary["total_reviews"] == 0
        assert summary["total_cost_usd"] == 0.0

    async def test_cost_summary_with_review(self, db):
        job, _task = await db.create_review_job("proj", "https://example.com/mr/1", "1")
        agent = Agent(model="gpt-4o", job_id=job.id, cost_usd=0.15, input_tokens=1000, output_tokens=500)
        await db.create_agent(agent)
        summary = await db.get_cost_summary()
        assert summary["total_reviews"] == 1
        assert summary["total_cost_usd"] == 0.15
