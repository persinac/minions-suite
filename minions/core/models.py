"""Pydantic models and enums for job orchestration and agent management."""

import uuid
from datetime import UTC, datetime
from enum import StrEnum

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

    # Owns only the git sequence: branch, commit, push, open PR, report it.
    # That work is mechanical and needs almost no context, but it sat at the end
    # of the engineer's turn budget and was the first thing starved when the
    # engineer ran long — repeatedly producing finished edits and no PR.
    FINISHER = "finisher"

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
    return datetime.now(UTC).isoformat()


class Agent(BaseModel):
    """Tracks a single agent invocation (LLM call session)."""

    id: str = Field(default_factory=_short_id)
    review_id: str | None = None
    model: str
    status: str = "starting"
    started_at: str = Field(default_factory=_now)
    finished_at: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cost_usd: float = 0.0
    num_turns: int = 0
    log_file: str | None = None
    error: str | None = None
    # Job orchestration fields (optional, used when agent is part of a job)
    job_id: str | None = None
    role: AgentRole | None = None
    task_id: str | None = None
    k8s_job_name: str | None = None


# ---------------------------------------------------------------------------
# Job orchestration models
# ---------------------------------------------------------------------------


class Job(BaseModel):
    """A feature specification job that drives the multi-agent workflow."""

    id: str = Field(default_factory=_short_id)
    spec: str = Field(..., description="The feature specification text")
    status: JobStatus = JobStatus.SPEC_RECEIVED
    job_type: str = Field(default="development", description="Job type: 'development' or 'review'")
    mr_url: str | None = Field(default=None, description="MR/PR URL (for review jobs)")
    error: str | None = None
    external_id: str | None = None
    difficulty: str | None = Field(
        default=None,
        description="easy | medium | hard, set by the classifier; selects the model tier for every agent on this job. None = unclassified, use the default model.",
    )
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
    branch_name: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    review_status: str | None = None
    deploy_status: str | None = None
    revision_count: int = 0
    attempt: int = 1
    max_attempts: int = 3
    error: str | None = None
    # Review task fields
    mr_url: str | None = Field(default=None, description="MR/PR URL (for review tasks)")
    mr_id: str | None = Field(default=None, description="MR/PR identifier (for review tasks)")
    specialty: str | None = Field(
        default=None,
        description="Expert reviewer specialty (api, dba, frontend, ...). None = the single general reviewer.",
    )
    verdict: str | None = Field(default=None, description="Review verdict: approve or request_changes")
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
    started_at: str | None = None
    completed_at: str | None = None
    result: dict | None = None
    error: str | None = None
    created_at: str = Field(default_factory=_now)


class Message(BaseModel):
    """An inter-agent message within a job."""

    id: str = Field(default_factory=_short_id)
    job_id: str
    from_role: AgentRole
    to_role: AgentRole | None = None
    content: str
    created_at: str = Field(default_factory=_now)
