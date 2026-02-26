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
from typing import Optional

from .models import Review
from .project_registry import ProjectConfig, ReviewProfile, infer_profile

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


def _load(relative_path: str) -> Optional[str]:
    """Load a prompt file relative to the prompts directory."""
    path = PROMPTS_DIR / relative_path
    if not path.exists():
        logger.warning("Prompt file not found: %s", path)
        return None
    return path.read_text(encoding="utf-8").strip()


def build_prompt(
    review: Review,
    project: ProjectConfig,
    changed_files: list[str],
) -> str:
    """Assemble the full review prompt from composable sections.

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
    context = _build_review_context(review, project, changed_files)
    sections.append(context)

    return "\n\n---\n\n".join(sections)


def _build_review_context(
    review: Review,
    project: ProjectConfig,
    changed_files: list[str],
) -> str:
    """Build the runtime context section injected at the end of the prompt."""
    ignore_note = ""
    if project.ignore_paths:
        ignore_note = f"\n- Ignore paths: {', '.join(project.ignore_paths)}"

    auto_approve_note = ""
    if project.auto_approve_paths:
        auto_approve_note = (
            f"\n- Auto-approve paths (approve quickly if ONLY these changed): {', '.join(project.auto_approve_paths)}"
        )

    files_list = "\n".join(f"  - {f}" for f in changed_files[:50])
    if len(changed_files) > 50:
        files_list += f"\n  - ... and {len(changed_files) - 50} more files"

    return f"""## Review Context

- Review ID: `{review.id}`
- Project: `{project.name}` ({project.git_provider})
- MR/PR: {review.mr_url}
- Branch: `{review.branch or 'unknown'}` -> `target`
- Title: {review.title or 'N/A'}
- Author: {review.author or 'N/A'}{ignore_note}{auto_approve_note}

### Changed Files
{files_list}"""
