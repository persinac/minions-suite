"""Tests for Pydantic models and enums."""

import re

from minions.core.models import (
    Agent,
    AgentRole,
    GitProvider,
    Job,
    JobStatus,
    Message,
    Subtask,
    SubtaskStatus,
    Task,
    TaskReviewStatus,
    TaskStatus,
    _now,
    _short_id,
)

# -----------------------------------------------------------------------
# Enum membership
# -----------------------------------------------------------------------


class TestEnums:
    def test_git_provider_values(self):
        assert set(GitProvider) == {"gitlab", "github", "bitbucket"}

    def test_job_status_count(self):
        assert len(JobStatus) == 12

    def test_job_status_has_terminal_states(self):
        assert "done" in JobStatus.__members__.values()
        assert "failed" in JobStatus.__members__.values()
        assert "no_work_needed" in JobStatus.__members__.values()

    def test_task_status_count(self):
        # 9 since no_work_needed was added: an engineer that finds the requested
        # change already present needs a terminal state that is not a failure.
        assert len(TaskStatus) == 9

    def test_task_status_has_no_work_needed(self):
        """Mirrors the JobStatus assertion above. Tasks lacked this for a long
        time, which is why a correct "already done" could only be recorded as an
        incomplete task and retried to failure."""
        assert "no_work_needed" in TaskStatus.__members__.values()

    def test_subtask_status_values(self):
        assert set(SubtaskStatus) == {"pending", "running", "completed", "failed"}

    def test_agent_role_values(self):
        expected = {
            "spec_analyst",
            "arbiter",
            "backend_engineer",
            "frontend_engineer",
            "database_engineer",
            "code_reviewer",
            "deploy_monitor",
            # Owns only the git sequence. A new role needs a prompt, a tool set
            # and a model tier, which is why this set is asserted exactly.
            "finisher",
        }
        assert expected == {r.value for r in AgentRole}

    def test_orchestrator_is_arbiter_alias(self):
        assert AgentRole.ORCHESTRATOR is AgentRole.ARBITER
        assert AgentRole.ORCHESTRATOR.value == "arbiter"

    def test_task_review_status_values(self):
        expected = {"pending_review", "approved", "changes_requested", "revision_in_progress", "revision_complete"}
        assert expected == {s.value for s in TaskReviewStatus}

    def test_str_enum_string_coercion(self):
        assert str(JobStatus.DONE) == "done"
        assert str(AgentRole.BACKEND_ENGINEER) == "backend_engineer"
        assert f"status={TaskStatus.PENDING}" == "status=pending"


# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------


class TestHelpers:
    def test_short_id_length(self):
        sid = _short_id()
        assert len(sid) == 8

    def test_short_id_hex_chars(self):
        sid = _short_id()
        assert re.fullmatch(r"[0-9a-f]{8}", sid)

    def test_short_id_unique(self):
        ids = {_short_id() for _ in range(100)}
        assert len(ids) == 100

    def test_now_is_iso_utc(self):
        ts = _now()
        assert "T" in ts
        assert ts.endswith("+00:00")


# -----------------------------------------------------------------------
# Model construction
# -----------------------------------------------------------------------


class TestAgent:
    def test_defaults(self):
        a = Agent(model="gpt-4o")
        assert len(a.id) == 8
        assert a.status == "starting"
        assert a.cost_usd == 0.0
        assert a.input_tokens == 0
        assert a.job_id is None
        assert a.role is None

    def test_with_job_fields(self):
        a = Agent(model="claude-3", job_id="j1", role=AgentRole.BACKEND_ENGINEER, task_id="t1")
        assert a.job_id == "j1"
        assert a.role == AgentRole.BACKEND_ENGINEER
        assert a.task_id == "t1"


class TestJob:
    def test_defaults(self):
        j = Job(spec="Build a widget")
        assert j.status == JobStatus.SPEC_RECEIVED
        assert j.job_type == "development"
        assert j.mr_url is None
        assert j.error is None
        assert len(j.id) == 8

    def test_review_job(self):
        j = Job(spec="review", status=JobStatus.TASKS_CREATED, job_type="review", mr_url="https://example.com/mr/1")
        assert j.job_type == "review"
        assert j.mr_url == "https://example.com/mr/1"

    def test_timestamps_populated(self):
        j = Job(spec="test")
        assert j.created_at
        assert j.updated_at


class TestTask:
    def test_required_fields(self):
        t = Task(job_id="j1", title="Do thing", service="api", agent_role=AgentRole.BACKEND_ENGINEER)
        assert t.status == TaskStatus.PENDING
        assert t.attempt == 1
        assert t.max_attempts == 3
        assert t.revision_count == 0

    def test_review_task_fields(self):
        t = Task(
            job_id="j1",
            title="Review",
            service="api",
            agent_role=AgentRole.CODE_REVIEWER,
            mr_url="https://example.com/mr/1",
            mr_id="1",
        )
        assert t.mr_url == "https://example.com/mr/1"
        assert t.mr_id == "1"
        assert t.verdict is None
        assert t.comments_posted == 0


class TestSubtask:
    def test_defaults(self):
        s = Subtask(task_id="t1", sequence_num=1, description="Step 1")
        assert s.status == SubtaskStatus.PENDING
        assert s.result is None
        assert s.error is None

    def test_with_result(self):
        s = Subtask(task_id="t1", sequence_num=2, description="Step 2", result={"key": "value"})
        assert s.result == {"key": "value"}


class TestMessage:
    def test_construction(self):
        m = Message(job_id="j1", from_role=AgentRole.SPEC_ANALYST, content="Hello")
        assert m.to_role is None
        assert m.content == "Hello"
        assert len(m.id) == 8

    def test_directed_message(self):
        m = Message(job_id="j1", from_role=AgentRole.ARBITER, to_role=AgentRole.BACKEND_ENGINEER, content="Fix bug")
        assert m.to_role == AgentRole.BACKEND_ENGINEER
