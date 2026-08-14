"""Legal state transition maps and validation for jobs, tasks, and subtasks."""


class InvalidTransitionError(Exception):
    """Raised when a state transition is not allowed.

    Carries the legal targets when the caller knows them. A refusal that only
    says "no" leaves an agent to guess, and agents guess badly: on job 7b840e7f
    one tried `completed`, then `done`, then `pr_created` in fifteen seconds,
    never learning that the answer was `pr_open`. Naming the alternatives turns
    three blind retries into one corrected call.
    """

    def __init__(self, entity_type: str, entity_id: str, from_status: str, to_status: str, allowed: set[str] | None = None):
        self.entity_type = entity_type
        self.entity_id = entity_id
        self.from_status = from_status
        self.to_status = to_status
        self.allowed = allowed
        message = f"Invalid {entity_type} transition for {entity_id}: {from_status} -> {to_status}"
        if allowed:
            message += f" (from {from_status}, allowed: {', '.join(sorted(allowed))})"
        elif allowed is not None:
            message += f" ({from_status} is terminal — no transitions allowed)"
        super().__init__(message)


class ArbiterUnavailableError(Exception):
    """Raised when the Arbiter refused a transition without judging it illegal.

    Distinct from InvalidTransitionError on purpose. "Your transition is not
    allowed" is permanent and the caller must change what it asks for; "the
    Arbiter would not answer right now" is transient and the caller should wait
    and repeat the same request.

    Collapsing the two cost job 7b840e7f its PR. A circuit-breaker refusal
    carries no from_status, so the old code substituted "?" and raised
    InvalidTransitionError -- reporting `? -> pr_open`, a transition error, for
    what was really a 300s cooldown. The agent read it as a permanent refusal of
    a PR it had genuinely opened, and stopped.
    """

    def __init__(self, entity_type: str, entity_id: str, to_status: str, reason: str, retry_after_seconds: int | None = None):
        self.entity_type = entity_type
        self.entity_id = entity_id
        self.to_status = to_status
        self.reason = reason
        self.retry_after_seconds = retry_after_seconds
        detail = f"Arbiter refused {entity_type} {entity_id} -> {to_status}: {reason}"
        if retry_after_seconds:
            detail += f" (retry in {retry_after_seconds}s)"
        super().__init__(detail)


class PreconditionError(Exception):
    """Raised when a task transition is missing required fields."""

    def __init__(self, task_id: str, to_status: str, missing_fields: list[str]):
        self.task_id = task_id
        self.to_status = to_status
        self.missing_fields = missing_fields
        super().__init__(f"Task {task_id} -> {to_status} missing required fields: {', '.join(missing_fields)}")


# -- Job transitions --
# Maps each JobStatus value to the set of allowed next states.
JOB_TRANSITIONS: dict[str, set[str]] = {
    "spec_received": {"spec_ready", "done", "failed"},
    "spec_ready": {"tasks_created", "failed"},
    "tasks_created": {"dev_in_progress", "review_in_progress", "no_work_needed", "failed"},
    # no_work_needed is reachable from dev_in_progress too: "there is nothing to
    # do here" is a conclusion an engineer reaches by READING the code, which
    # happens after dispatch, not before it. Allowing it only from
    # tasks_created meant the discovery could never be recorded once work had
    # started -- which is the only time it is ever actually made.
    "dev_in_progress": {"pr_open", "merged", "no_work_needed", "failed"},
    "pr_open": {"review_in_progress", "in_progress", "failed"},
    "review_in_progress": {"tasks_created", "merged", "done", "failed"},
    "merged": {"deploying", "deployed", "failed"},
    "deploying": {"deployed", "failed"},
    "deployed": {"done", "failed"},
    # Terminal states: no further transitions
    "done": set(),
    "no_work_needed": set(),
    # Allow recovery from failed (arbiter can reset job to tasks_created for retry)
    "failed": {"tasks_created"},
}

# -- Task transitions (default) --
# Used for roles without a role-specific override in ROLE_TASK_TRANSITIONS.
TASK_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"in_progress", "failed"},
    "in_progress": {"pr_open", "merged", "done", "no_work_needed", "failed"},
    "pr_open": {"in_review", "in_progress", "merged", "done", "failed"},
    "in_review": {"merged", "in_progress", "pr_open", "failed"},
    "merged": {"deploying", "done", "failed"},
    "deploying": {"done", "failed"},
    # Terminal states
    "done": set(),
    # Terminal, and deliberately NOT reachable from pr_open or in_review: once a
    # PR exists the work plainly was needed, and letting a task retreat to
    # "nothing to do" from there would strand an open PR the way job 7b840e7f's
    # was stranded.
    "no_work_needed": set(),
    # Allow retry: arbiter can reset failed tasks to pending
    "failed": {"pending"},
}

# -- Role-specific task transitions --
# Roles listed here use their own transition map instead of the default TASK_TRANSITIONS.
# Database engineer: no PR review cycle, no deploy phase. Work goes straight to done.
ROLE_TASK_TRANSITIONS: dict[str, dict[str, set[str]]] = {
    "database_engineer": {
        "pending": {"in_progress", "failed"},
        "in_progress": {"merged", "done", "no_work_needed", "failed"},
        "merged": {"done", "failed"},
        "done": set(),
        "no_work_needed": set(),
        "failed": {"pending"},
    },
}

# -- Role-restricted task transitions --
# Maps (from_status, to_status) to the set of AgentRole values allowed to make that transition.
# If a transition is NOT listed here, any role can make it (no restriction).
# If a transition IS listed here, only the specified roles may perform it.
ROLE_RESTRICTED_TASK_TRANSITIONS: dict[tuple[str, str], set[str]] = {
    ("in_progress", "merged"): {"database_engineer"},
    # Engineers cannot skip code review by marking tasks done directly (database_engineer exempt via role matrix)
    ("in_progress", "done"): {"code_reviewer", "deploy_monitor", "database_engineer"},
    # Only the code_reviewer (approve+merge) or engine (edge cases) should close a PR task.
    ("pr_open", "done"): {"code_reviewer", "deploy_monitor"},
    # Only the code_reviewer can merge PRs
    ("pr_open", "merged"): {"code_reviewer"},
    ("in_review", "merged"): {"code_reviewer"},
    # Only the arbiter can retry failed tasks
    ("failed", "pending"): {"arbiter"},
}

# -- Subtask transitions --
SUBTASK_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"running", "failed"},
    "running": {"completed", "failed"},
    # Terminal states
    "completed": set(),
    # Allow retry
    "failed": {"pending"},
}


def validate_job_transition(job_id: str, from_status: str, to_status: str) -> None:
    """Raise InvalidTransitionError if the job transition is not allowed."""
    allowed = JOB_TRANSITIONS.get(from_status, set())
    if to_status not in allowed:
        raise InvalidTransitionError("job", job_id, from_status, to_status)


def _get_task_transitions(agent_role: str) -> dict[str, set[str]]:
    """Return the task transition map for a given role (role-specific or default)."""
    if agent_role:
        role_map = ROLE_TASK_TRANSITIONS.get(agent_role)
        if role_map is not None:
            return role_map
    return TASK_TRANSITIONS


def validate_task_transition(task_id: str, from_status: str, to_status: str, agent_role: str = "") -> None:
    """Raise InvalidTransitionError if the task transition is not allowed."""
    transitions = _get_task_transitions(agent_role)
    allowed = transitions.get(from_status, set())
    if to_status not in allowed:
        raise InvalidTransitionError("task", task_id, from_status, to_status, allowed=allowed)
    # Check role restrictions (only applies to default transitions)
    restricted_roles = ROLE_RESTRICTED_TASK_TRANSITIONS.get((from_status, to_status))
    if restricted_roles and agent_role and agent_role not in restricted_roles:
        # Legal transition, wrong caller. Report the roles that may make it
        # rather than the target states, or the agent reads "pr_open -> done is
        # not allowed" and starts hunting for a different target when the real
        # answer is that a reviewer performs this one.
        raise InvalidTransitionError(
            "task",
            task_id,
            from_status,
            f"{to_status} (restricted to roles: {', '.join(sorted(restricted_roles))}; caller is {agent_role!r})",
        )


def validate_subtask_transition(subtask_id: str, from_status: str, to_status: str) -> None:
    """Raise InvalidTransitionError if the subtask transition is not allowed."""
    allowed = SUBTASK_TRANSITIONS.get(from_status, set())
    if to_status not in allowed:
        raise InvalidTransitionError("subtask", subtask_id, from_status, to_status)


# -- Task Preconditions --
# Maps a target task status to the set of task fields that must be non-null before entering that status.
TASK_PRECONDITIONS: dict[str, set[str]] = {
    "pr_open": {"pr_url", "pr_number", "branch_name"},
    "deploying": {"branch_name"},
    "merged": {"pr_url"},
}


def validate_task_preconditions(task_id: str, to_status: str, task_data: dict, agent_role: str = "") -> None:
    """Raise PreconditionError if the task is missing required fields for the target status."""
    required_fields = TASK_PRECONDITIONS.get(to_status)
    if not required_fields:
        return
    # Database engineers merge directly (no PR), so pr_url/pr_number are not required
    if agent_role == "database_engineer":
        required_fields = required_fields - {"pr_url", "pr_number"}
    if not required_fields:
        return
    missing = [f for f in required_fields if not task_data.get(f)]
    if missing:
        raise PreconditionError(task_id, to_status, missing)


# -- Review Status Validation --
VALID_REVIEW_STATUSES: set[str] = {
    "pending_review",
    "approved",
    "changes_requested",
    "revision_in_progress",
    "revision_complete",
}


def validate_review_status(review_status: str | None) -> None:
    """Raise ValueError if review_status is not a recognized value (None is always valid)."""
    if review_status is not None and review_status not in VALID_REVIEW_STATUSES:
        raise ValueError(f"Invalid review_status: {review_status!r} (allowed: {VALID_REVIEW_STATUSES})")
