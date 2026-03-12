"""Composable prompt assembly from markdown mixins.

Prompts are built by layering:
  1. base.md          — universal review checklist (always loaded)
  2. roles/*.md       — domain expertise (backend, frontend, data_engineer, devops, security)
  3. languages/*.md   — language-specific rules (python, typescript, go, sql, shell)
  4. custom/*.md      — org/team/project-specific overrides
  5. Review context   — MR metadata, changed files, branch info
"""

import logging
from pathlib import Path

from ..core.models import Job, Task
from ..project_registry import ProjectConfig, ServiceTarget, infer_profile

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent.parent.parent / "prompts"


def _load(relative_path: str) -> str | None:
    """Load a prompt file relative to the prompts directory."""
    path = PROMPTS_DIR / relative_path
    if not path.exists():
        logger.warning("Prompt file not found: %s", path)
        return None
    return path.read_text(encoding="utf-8").strip()


def build_review_prompt(
    task: Task,
    project: ProjectConfig,
    changed_files: list[str],
) -> str:
    """Assemble the full review prompt from composable sections.

    Accepts a Task (with mr_url/mr_id fields) instead of the old Review model.
    If the project has no explicit review_profile, infer one from changed files.
    """
    sections: list[str] = []

    # 1. Base checklist (always)
    base = _load("base.md")
    if base:
        sections.append(base)

    # 2. Determine profile
    profile = project.review_profile
    if not profile.roles and not profile.languages:
        profile = infer_profile(changed_files)
        logger.info("Auto-inferred review profile: roles=%s, languages=%s", profile.roles, profile.languages)

    # 3. Role mixins
    for role in profile.roles:
        content = _load(f"roles/{role}.md")
        if content:
            sections.append(content)

    # 4. Language mixins
    for lang in profile.languages:
        content = _load(f"languages/{lang}.md")
        if content:
            sections.append(content)

    # 5. Custom rules
    for custom in profile.custom:
        content = _load(f"custom/{custom}")
        if content:
            sections.append(content)

    # 6. Review context
    context = _build_review_context(task, project, changed_files)
    sections.append(context)

    return "\n\n---\n\n".join(sections)


def _build_review_context(
    task: Task,
    project: ProjectConfig,
    changed_files: list[str],
) -> str:
    """Build the runtime context section injected at the end of the prompt."""
    ignore_note = ""
    if project.ignore_paths:
        ignore_note = f"\n- Ignore paths: {', '.join(project.ignore_paths)}"

    auto_approve_note = ""
    if project.auto_approve_paths:
        auto_approve_note = f"\n- Auto-approve paths (approve quickly if ONLY these changed): {', '.join(project.auto_approve_paths)}"

    files_list = "\n".join(f"  - {f}" for f in changed_files[:50])
    if len(changed_files) > 50:
        files_list += f"\n  - ... and {len(changed_files) - 50} more files"

    return f"""## Review Context

- Task ID: `{task.id}`
- Project: `{project.name}` ({project.git_provider})
- MR/PR: {task.mr_url or "N/A"}
- MR ID: {task.mr_id or "N/A"}{ignore_note}{auto_approve_note}

### Changed Files
{files_list}"""


# Backward compat alias
build_prompt = build_review_prompt


# ---------------------------------------------------------------------------
# Agent prompt assembly (job orchestration)
# ---------------------------------------------------------------------------

# Map agent_role to prompt template file
_ROLE_TO_PROMPT: dict[str, str] = {
    "spec_analyst": "agents/spec_analyst.md",
    "arbiter": "agents/arbiter.md",
    "backend_engineer": "agents/engineer.md",
    "frontend_engineer": "agents/engineer.md",
    "database_engineer": "agents/engineer.md",
    "code_reviewer": "agents/spec_analyst.md",  # reuse base analysis prompt
    "deploy_monitor": "agents/deployer.md",
}

# Map agent_role to language inference
_ROLE_TO_LANGUAGE: dict[str, str] = {
    "backend_engineer": "python",
    "frontend_engineer": "typescript",
    "database_engineer": "sql",
}


def build_agent_prompt(
    job: Job,
    task: Task,
    project: ProjectConfig | None = None,
    service: ServiceTarget | None = None,
    context: str | None = None,
) -> str:
    """Assemble a prompt for a job orchestration agent.

    Selects the agent prompt template based on task.agent_role,
    then layers on role and language mixins plus task context.
    """
    sections: list[str] = []

    # 1. Agent-specific base prompt
    prompt_file = _ROLE_TO_PROMPT.get(task.agent_role, "agents/engineer.md")
    agent_base = _load(prompt_file)
    if agent_base:
        sections.append(agent_base)

    # 2. Role mixins (if applicable)
    role = task.agent_role
    if role in ("backend_engineer", "frontend_engineer"):
        role_name = role.replace("_engineer", "")
        content = _load(f"roles/{role_name}.md")
        if content:
            sections.append(content)

    # 3. Language mixin
    lang = _ROLE_TO_LANGUAGE.get(role, "")
    if service and service.language:
        lang = service.language
    if lang:
        content = _load(f"languages/{lang}.md")
        if content:
            sections.append(content)

    # 4. Task context
    task_context = _build_task_context(job, task, project, service)
    sections.append(task_context)

    # 5. Additional context (e.g. checkpoint summary for retries)
    if context:
        sections.append(context)

    return "\n\n---\n\n".join(sections)


def _build_task_context(
    job: Job,
    task: Task,
    project: ProjectConfig | None = None,
    service: ServiceTarget | None = None,
) -> str:
    """Build the runtime context section for an agent prompt."""
    lines = [
        "## Task Context",
        "",
        f"- Job ID: `{job.id}`",
        f"- Task ID: `{task.id}`",
        f"- Task: {task.title}",
        f"- Service: `{task.service}`",
        f"- Role: `{task.agent_role}`",
        f"- Attempt: {task.attempt}/{task.max_attempts}",
    ]

    if task.description:
        lines.append(f"\n### Description\n{task.description}")

    if project:
        lines.append(f"\n- Project: `{project.name}`")

    if service:
        if service.clone_url:
            lines.append(f"- Repo: `{service.clone_url}`")
        if service.default_branch:
            lines.append(f"- Default branch: `{service.default_branch}`")
        if service.test_command:
            lines.append(f"- Test command: `{service.test_command}`")
        if service.lint_command:
            lines.append(f"- Lint command: `{service.lint_command}`")
        if service.framework:
            lines.append(f"- Framework: `{service.framework}`")

    if task.branch_name:
        lines.append(f"- Branch: `{task.branch_name}`")

    lines.append(f"\n### Spec\n{job.spec}")

    return "\n".join(lines)
