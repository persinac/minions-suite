"""Multi-project configuration from projects.yaml."""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)


@dataclass
class ReviewProfile:
    """Composable review profile: roles + languages + custom rules."""

    roles: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    custom: list[str] = field(default_factory=list)


@dataclass
class ProjectConfig:
    """Configuration for a single project."""

    name: str
    project_id: str  # e.g. "team/payments-api" (GitLab) or "org/repo" (GitHub)
    git_provider: str = "gitlab"
    gitlab_url: str = ""
    repo_path: str = ""  # local clone path (optional, for watch mode)
    model: str = ""  # override per-project, empty = use default
    review_profile: ReviewProfile = field(default_factory=ReviewProfile)
    ignore_paths: list[str] = field(default_factory=list)
    auto_approve_paths: list[str] = field(default_factory=list)


class ProjectRegistryError(Exception):
    pass


def _parse_profile(raw: dict) -> ReviewProfile:
    """Parse a review_profile section from YAML."""
    if not raw:
        return ReviewProfile()
    return ReviewProfile(
        roles=raw.get("roles", []),
        languages=raw.get("languages", []),
        custom=raw.get("custom", []),
    )


def build_registry(config_path: str) -> dict[str, ProjectConfig]:
    """Load project registry from YAML config file.

    Returns a dict of project name -> ProjectConfig.
    """
    path = Path(config_path)
    if not path.exists():
        raise ProjectRegistryError(f"Projects config not found: {config_path}")

    with open(path) as f:
        data = yaml.safe_load(f) or {}

    defaults = data.get("defaults", {})
    default_model = defaults.get("model", "gpt-4o")
    default_provider = defaults.get("git_provider", "gitlab")
    default_gitlab_url = defaults.get("gitlab_url", "")

    projects_data = data.get("projects", {})
    if not projects_data:
        logger.warning("No projects defined in %s", config_path)
        return {}

    registry: dict[str, ProjectConfig] = {}
    for name, proj in projects_data.items():
        if proj is None:
            continue
        registry[name] = ProjectConfig(
            name=name,
            project_id=proj.get("project_id", name),
            git_provider=proj.get("git_provider", default_provider),
            gitlab_url=proj.get("gitlab_url", default_gitlab_url),
            repo_path=proj.get("repo_path", ""),
            model=proj.get("model", default_model),
            review_profile=_parse_profile(proj.get("review_profile", {})),
            ignore_paths=proj.get("ignore_paths", []),
            auto_approve_paths=proj.get("auto_approve_paths", []),
        )

    logger.info("Loaded %d projects from %s", len(registry), config_path)
    return registry


def infer_profile(changed_files: list[str]) -> ReviewProfile:
    """Auto-detect review profile from changed file paths.

    Used as a fallback when a project doesn't have explicit review_profile config.
    """
    languages: set[str] = set()
    roles: set[str] = set()

    for f in changed_files:
        fl = f.lower()

        # Language detection
        if fl.endswith(".py"):
            languages.add("python")
        if fl.endswith((".ts", ".tsx")):
            languages.add("typescript")
        if fl.endswith((".js", ".jsx")):
            languages.add("typescript")  # TS rules are a superset
        if fl.endswith(".go"):
            languages.add("go")
        if fl.endswith(".sql"):
            languages.add("sql")
        if fl.endswith((".sh", ".bash")):
            languages.add("shell")

        # Role detection
        if fl.endswith(".sql") or "migration" in fl or "alembic" in fl:
            roles.add("data_engineer")
        if "dockerfile" in fl or ".github/" in fl or ".gitlab-ci" in fl or "ci/" in fl:
            roles.add("devops")
        if "/api/" in fl or "endpoints/" in fl or "routes/" in fl or "views/" in fl:
            roles.add("backend")
        if "/components/" in fl or "/pages/" in fl or "/app/" in fl or "src/ui/" in fl:
            roles.add("frontend")

    return ReviewProfile(roles=sorted(roles), languages=sorted(languages))
