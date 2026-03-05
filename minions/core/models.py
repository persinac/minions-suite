"""Pydantic models and enums for job orchestration and agent management."""

import uuid
from datetime import datetime, timezone
from enum import StrEnum
from typing import Optional

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class GitProvider(StrEnum):
    GITLAB = "gitlab"
    GITHUB = "github"
    BITBUCKET = "bitbucket"


# ---------------------------------------------------------------------------
# Job orchestration enums
# ---------------------------------------------------------------------------


class JobStatus(StrEnum):
    SPEC_RECEIVED = "spec_received"
    SPEC_READY = "spec_ready"
    TASKS_CREATED = "tasks_created"
    DEV_IN_PROGRESS = "dev_in_progress"
    PR_OPEN = "pr_open"
    REVIEW_IN_PROGRESS = "review_in_progress"
    MERGED = "merged"
    DEPLOYING = "deploying"
    DEPLOYED = "deployed"
    DONE = "done"
    NO_WORK_NEEDED = "no_work_needed"
    FAILED = "failed"


class TaskStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    PR_OPEN = "pr_open"
    IN_REVIEW = "in_review"
    MERGED = "merged"
    DEPLOYING = "deploying"
    DONE = "done"
    FAILED = "failed"


class SubtaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class AgentRole(StrEnum):
    SPEC_ANALYST = "spec_analyst"
    ARBITER = "arbiter"
    BACKEND_ENGINEER = "backend_engineer"
    FRONTEND_ENGINEER = "frontend_engineer"
    DATABASE_ENGINEER = "database_engineer"
    CODE_REVIEWER = "code_reviewer"
    DEPLOY_MONITOR = "deploy_monitor"

    # Alias: ORCHESTRATOR resolves to ARBITER (same value = enum alias)
    ORCHESTRATOR = "arbiter"


class RiskLevel(StrEnum):
    PATCH = "patch"
    MINOR = "minor"
    MAJOR = "major"
    UNKNOWN = "unknown"


class TaskReviewStatus(StrEnum):
    """Review status within a task lifecycle (distinct from ReviewStatus used for standalone reviews)."""

    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"
    REVISION_IN_PROGRESS = "revision_in_progress"
    REVISION_COMPLETE = "revision_complete"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _short_id() -> str:
    return uuid.uuid4().hex[:8]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Agent(BaseModel):
    """Tracks a single agent invocation (LLM call session)."""

    id: str = Field(default_factory=_short_id)
    review_id: Optional[str] = None
    model: str
    status: str = "starting"
    started_at: str = Field(default_factory=_now)
    finished_at: Optional[str] = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cost_usd: float = 0.0
    num_turns: int = 0
    log_file: Optional[str] = None
    error: Optional[str] = None
    # Job orchestration fields (optional, used when agent is part of a job)
    job_id: Optional[str] = None
    role: Optional[AgentRole] = None
    task_id: Optional[str] = None
    k8s_job_name: Optional[str] = None


# ---------------------------------------------------------------------------
# Job orchestration models
# ---------------------------------------------------------------------------


class Job(BaseModel):
    """A feature specification job that drives the multi-agent workflow."""

    id: str = Field(default_factory=_short_id)
    spec: str = Field(..., description="The feature specification text")
    status: JobStatus = JobStatus.SPEC_RECEIVED
    job_type: str = Field(default="development", description="Job type: 'development' or 'review'")
    mr_url: Optional[str] = Field(default=None, description="MR/PR URL (for review jobs)")
    error: Optional[str] = None
    external_id: Optional[str] = None
    created_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)


class Task(BaseModel):
    """A per-service work item within a job."""

    id: str = Field(default_factory=_short_id)
    job_id: str = Field(..., description="Parent job ID")
    title: str = Field(..., description="Short task title")
    description: str = Field(default="", description="Detailed task description")
    service: str = Field(..., description="Target service name")
    agent_role: AgentRole = Field(..., description="Which agent role handles this")
    status: TaskStatus = TaskStatus.PENDING
    branch_name: Optional[str] = None
    pr_number: Optional[int] = None
    pr_url: Optional[str] = None
    review_status: Optional[str] = None
    deploy_status: Optional[str] = None
    revision_count: int = 0
    attempt: int = 1
    max_attempts: int = 3
    error: Optional[str] = None
    # Review task fields
    mr_url: Optional[str] = Field(default=None, description="MR/PR URL (for review tasks)")
    mr_id: Optional[str] = Field(default=None, description="MR/PR identifier (for review tasks)")
    verdict: Optional[str] = Field(default=None, description="Review verdict: approve or request_changes")
    comments_posted: int = Field(default=0, description="Number of inline comments posted")
    created_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)


class Subtask(BaseModel):
    """A granular step within a task, tracked by the agent."""

    id: str = Field(default_factory=_short_id)
    task_id: str = Field(..., description="Parent task ID")
    sequence_num: int = Field(..., description="Order within the parent task")
    description: str = Field(..., description="What this subtask does")
    status: SubtaskStatus = SubtaskStatus.PENDING
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    result: Optional[dict] = None
    error: Optional[str] = None
    created_at: str = Field(default_factory=_now)


class Message(BaseModel):
    """An inter-agent message within a job."""

    id: str = Field(default_factory=_short_id)
    job_id: str
    from_role: AgentRole
    to_role: Optional[AgentRole] = None
    content: str
    created_at: str = Field(default_factory=_now)
