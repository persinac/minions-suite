"""Pydantic models and enums for PR review orchestration."""

import uuid
from datetime import datetime, timezone
from enum import StrEnum
from typing import Optional

from pydantic import BaseModel, Field


class ReviewStatus(StrEnum):
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    COMMENTED = "commented"
    DONE = "done"
    FAILED = "failed"


class ReviewVerdict(StrEnum):
    APPROVE = "approve"
    REQUEST_CHANGES = "request_changes"


class GitProvider(StrEnum):
    GITLAB = "gitlab"
    GITHUB = "github"
    BITBUCKET = "bitbucket"


def _short_id() -> str:
    return uuid.uuid4().hex[:8]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Review(BaseModel):
    """A single MR/PR review request."""

    id: str = Field(default_factory=_short_id)
    project: str = Field(..., description="Project key from projects.yaml")
    mr_url: str = Field(..., description="Full URL of the merge/pull request")
    mr_id: str = Field(..., description="MR/PR identifier (number or IID)")
    branch: Optional[str] = None
    title: Optional[str] = None
    author: Optional[str] = None
    status: ReviewStatus = ReviewStatus.QUEUED
    verdict: Optional[ReviewVerdict] = None
    summary: Optional[str] = None
    comments_posted: int = 0
    model: Optional[str] = None
    error: Optional[str] = None
    created_at: str = Field(default_factory=_now)
    started_at: Optional[str] = None
    completed_at: Optional[str] = None


class ReviewComment(BaseModel):
    """An inline comment left on a specific file/line."""

    id: str = Field(default_factory=_short_id)
    review_id: str
    file_path: str
    line: Optional[int] = None
    severity: str = "nit"  # critical, warning, nit
    body: str
    created_at: str = Field(default_factory=_now)


class Agent(BaseModel):
    """Tracks a single agent invocation (LLM call session)."""

    id: str = Field(default_factory=_short_id)
    review_id: str
    model: str
    status: str = "starting"
    started_at: str = Field(default_factory=_now)
    finished_at: Optional[str] = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    num_turns: int = 0
    log_file: Optional[str] = None
    error: Optional[str] = None
