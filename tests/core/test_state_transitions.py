"""Tests for state transition validation logic."""

import pytest

from minions.state_transitions import (
    JOB_TRANSITIONS,
    SUBTASK_TRANSITIONS,
    TASK_TRANSITIONS,
    VALID_REVIEW_STATUSES,
    InvalidTransitionError,
    PreconditionError,
    validate_job_transition,
    validate_review_status,
    validate_subtask_transition,
    validate_task_preconditions,
    validate_task_transition,
)

# -----------------------------------------------------------------------
# Job transitions
# -----------------------------------------------------------------------


class TestJobTransitions:
    def test_spec_received_to_spec_ready(self):
        validate_job_transition("j1", "spec_received", "spec_ready")

    def test_spec_received_to_done(self):
        validate_job_transition("j1", "spec_received", "done")

    def test_spec_received_to_failed(self):
        validate_job_transition("j1", "spec_received", "failed")

    def test_spec_ready_to_tasks_created(self):
        validate_job_transition("j1", "spec_ready", "tasks_created")

    def test_tasks_created_to_dev_in_progress(self):
        validate_job_transition("j1", "tasks_created", "dev_in_progress")

    def test_tasks_created_to_review_in_progress(self):
        validate_job_transition("j1", "tasks_created", "review_in_progress")

    def test_tasks_created_to_no_work_needed(self):
        validate_job_transition("j1", "tasks_created", "no_work_needed")

    def test_review_in_progress_to_done(self):
        validate_job_transition("j1", "review_in_progress", "done")

    def test_review_in_progress_to_merged(self):
        validate_job_transition("j1", "review_in_progress", "merged")

    def test_review_in_progress_to_tasks_created(self):
        validate_job_transition("j1", "review_in_progress", "tasks_created")

    def test_merged_to_deploying(self):
        validate_job_transition("j1", "merged", "deploying")

    def test_merged_to_deployed(self):
        validate_job_transition("j1", "merged", "deployed")

    def test_deploying_to_deployed(self):
        validate_job_transition("j1", "deploying", "deployed")

    def test_deployed_to_done(self):
        validate_job_transition("j1", "deployed", "done")

    def test_failed_to_tasks_created(self):
        validate_job_transition("j1", "failed", "tasks_created")

    def test_done_is_terminal(self):
        with pytest.raises(InvalidTransitionError):
            validate_job_transition("j1", "done", "spec_received")

    def test_no_work_needed_is_terminal(self):
        with pytest.raises(InvalidTransitionError):
            validate_job_transition("j1", "no_work_needed", "done")

    def test_invalid_backward_transition(self):
        with pytest.raises(InvalidTransitionError):
            validate_job_transition("j1", "deployed", "spec_received")

    def test_invalid_skip_transition(self):
        with pytest.raises(InvalidTransitionError):
            validate_job_transition("j1", "spec_received", "deployed")

    def test_error_has_correct_fields(self):
        with pytest.raises(InvalidTransitionError) as exc_info:
            validate_job_transition("j42", "done", "failed")
        err = exc_info.value
        assert err.entity_type == "job"
        assert err.entity_id == "j42"
        assert err.from_status == "done"
        assert err.to_status == "failed"

    def test_all_states_covered(self):
        """Every JobStatus value should have an entry in JOB_TRANSITIONS."""
        from minions.models import JobStatus
        for status in JobStatus:
            assert status.value in JOB_TRANSITIONS, f"Missing JOB_TRANSITIONS entry for {status}"


# -----------------------------------------------------------------------
# Task transitions (default)
# -----------------------------------------------------------------------


class TestTaskTransitions:
    def test_pending_to_in_progress(self):
        validate_task_transition("t1", "pending", "in_progress")

    def test_in_progress_to_pr_open(self):
        validate_task_transition("t1", "in_progress", "pr_open")

    def test_in_progress_to_done_by_reviewer(self):
        validate_task_transition("t1", "in_progress", "done", agent_role="code_reviewer")

    def test_in_progress_to_done_blocked_for_engineer(self):
        with pytest.raises(InvalidTransitionError):
            validate_task_transition("t1", "in_progress", "done", agent_role="backend_engineer")

    def test_pr_open_to_in_review(self):
        validate_task_transition("t1", "pr_open", "in_review")

    def test_in_review_to_merged_by_reviewer(self):
        validate_task_transition("t1", "in_review", "merged", agent_role="code_reviewer")

    def test_in_review_to_merged_blocked_for_engineer(self):
        with pytest.raises(InvalidTransitionError):
            validate_task_transition("t1", "in_review", "merged", agent_role="backend_engineer")

    def test_in_review_to_in_progress(self):
        validate_task_transition("t1", "in_review", "in_progress")

    def test_merged_to_deploying(self):
        validate_task_transition("t1", "merged", "deploying")

    def test_merged_to_done(self):
        validate_task_transition("t1", "merged", "done")

    def test_deploying_to_done(self):
        validate_task_transition("t1", "deploying", "done")

    def test_done_is_terminal(self):
        with pytest.raises(InvalidTransitionError):
            validate_task_transition("t1", "done", "in_progress")

    def test_failed_to_pending_by_arbiter(self):
        validate_task_transition("t1", "failed", "pending", agent_role="arbiter")

    def test_failed_to_pending_blocked_for_engineer(self):
        with pytest.raises(InvalidTransitionError):
            validate_task_transition("t1", "failed", "pending", agent_role="backend_engineer")

    def test_no_role_restriction_allows_any(self):
        """Transitions not in ROLE_RESTRICTED should work for any role."""
        validate_task_transition("t1", "pending", "in_progress", agent_role="backend_engineer")
        validate_task_transition("t1", "pending", "in_progress", agent_role="code_reviewer")

    def test_all_task_statuses_covered(self):
        from minions.models import TaskStatus
        for status in TaskStatus:
            assert status.value in TASK_TRANSITIONS, f"Missing TASK_TRANSITIONS entry for {status}"


# -----------------------------------------------------------------------
# Role-specific task transitions (database_engineer)
# -----------------------------------------------------------------------


class TestDatabaseEngineerTransitions:
    def test_in_progress_to_merged(self):
        validate_task_transition("t1", "in_progress", "merged", agent_role="database_engineer")

    def test_in_progress_to_done(self):
        validate_task_transition("t1", "in_progress", "done", agent_role="database_engineer")

    def test_no_pr_open_state(self):
        with pytest.raises(InvalidTransitionError):
            validate_task_transition("t1", "in_progress", "pr_open", agent_role="database_engineer")

    def test_no_deploying_state(self):
        with pytest.raises(InvalidTransitionError):
            validate_task_transition("t1", "merged", "deploying", agent_role="database_engineer")


# -----------------------------------------------------------------------
# Subtask transitions
# -----------------------------------------------------------------------


class TestSubtaskTransitions:
    def test_pending_to_running(self):
        validate_subtask_transition("s1", "pending", "running")

    def test_running_to_completed(self):
        validate_subtask_transition("s1", "running", "completed")

    def test_running_to_failed(self):
        validate_subtask_transition("s1", "running", "failed")

    def test_completed_is_terminal(self):
        with pytest.raises(InvalidTransitionError):
            validate_subtask_transition("s1", "completed", "running")

    def test_failed_to_pending_retry(self):
        validate_subtask_transition("s1", "failed", "pending")

    def test_pending_to_completed_invalid(self):
        with pytest.raises(InvalidTransitionError):
            validate_subtask_transition("s1", "pending", "completed")

    def test_all_subtask_statuses_covered(self):
        from minions.models import SubtaskStatus
        for status in SubtaskStatus:
            assert status.value in SUBTASK_TRANSITIONS, f"Missing SUBTASK_TRANSITIONS entry for {status}"


# -----------------------------------------------------------------------
# Task preconditions
# -----------------------------------------------------------------------


class TestTaskPreconditions:
    def test_pr_open_requires_fields(self):
        with pytest.raises(PreconditionError) as exc_info:
            validate_task_preconditions("t1", "pr_open", {})
        assert "pr_url" in exc_info.value.missing_fields
        assert "pr_number" in exc_info.value.missing_fields
        assert "branch_name" in exc_info.value.missing_fields

    def test_pr_open_satisfied(self):
        validate_task_preconditions("t1", "pr_open", {
            "pr_url": "https://example.com/pr/1",
            "pr_number": 1,
            "branch_name": "feat/x",
        })

    def test_deploying_requires_branch(self):
        with pytest.raises(PreconditionError):
            validate_task_preconditions("t1", "deploying", {})

    def test_deploying_satisfied(self):
        validate_task_preconditions("t1", "deploying", {"branch_name": "main"})

    def test_merged_requires_pr_url(self):
        with pytest.raises(PreconditionError):
            validate_task_preconditions("t1", "merged", {})

    def test_merged_satisfied(self):
        validate_task_preconditions("t1", "merged", {"pr_url": "https://example.com/pr/1"})

    def test_database_engineer_merged_no_pr_required(self):
        validate_task_preconditions("t1", "merged", {}, agent_role="database_engineer")

    def test_database_engineer_pr_open_no_pr_required(self):
        validate_task_preconditions("t1", "pr_open", {"branch_name": "feat/x"}, agent_role="database_engineer")

    def test_no_preconditions_for_pending(self):
        validate_task_preconditions("t1", "pending", {})

    def test_no_preconditions_for_in_progress(self):
        validate_task_preconditions("t1", "in_progress", {})

    def test_precondition_error_fields(self):
        with pytest.raises(PreconditionError) as exc_info:
            validate_task_preconditions("t42", "pr_open", {})
        err = exc_info.value
        assert err.task_id == "t42"
        assert err.to_status == "pr_open"


# -----------------------------------------------------------------------
# Review status validation
# -----------------------------------------------------------------------


class TestReviewStatus:
    def test_none_is_valid(self):
        validate_review_status(None)

    def test_all_valid_statuses(self):
        for status in VALID_REVIEW_STATUSES:
            validate_review_status(status)

    def test_invalid_status(self):
        with pytest.raises(ValueError):
            validate_review_status("invalid_status")

    def test_empty_string_is_invalid(self):
        with pytest.raises(ValueError):
            validate_review_status("")
