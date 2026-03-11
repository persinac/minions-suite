"""Multi-project configuration from projects.yaml."""

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


@dataclass
class ReviewProfile:
    """Composable review profile: roles + languages + custom rules."""

    roles: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    custom: list[str] = field(default_factory=list)


@dataclass
class IssuesConfig:
    """Configuration for GitLab issues-driven job creation."""

    enabled: bool = False
    label: str = "minions"


@dataclass
class AgentProfile:
    """Composable agent profile: roles, languages, custom rules, and overrides."""

    roles: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    custom: list[str] = field(default_factory=list)
    timeout: int = 600
    model: str = ""
    allowed_tools: list[str] = field(default_factory=list)


@dataclass
class ServiceTarget:
    """A deployable service within a project."""

    name: str
    project_id: str = ""
    git_provider: str = "gitlab"
    gitlab_url: str = ""
    repo_path: str = ""
    clone_url: str = ""
    ci_type: str = ""  # e.g. "circleci", "github_actions", "gitlab_ci"
    deploy_target: str = ""  # e.g. "apprunner", "k8s", "amplify"
    language: str = ""
    framework: str = ""
    test_command: str = ""
    lint_command: str = ""
    default_branch: str = "main"


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
    # Job orchestration profiles
    engineer_profile: AgentProfile = field(default_factory=AgentProfile)
    deploy_profile: AgentProfile = field(default_factory=AgentProfile)
    services: dict[str, ServiceTarget] = field(default_factory=dict)
    issues: IssuesConfig = field(default_factory=IssuesConfig)


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


def _parse_agent_profile(raw: dict) -> AgentProfile:
    """Parse an agent profile section from YAML."""
    if not raw:
        return AgentProfile()
    return AgentProfile(
        roles=raw.get("roles", []),
        languages=raw.get("languages", []),
        custom=raw.get("custom", []),
        timeout=raw.get("timeout", 600),
        model=raw.get("model", ""),
        allowed_tools=raw.get("allowed_tools", []),
    )


def _parse_issues_config(raw: dict) -> IssuesConfig:
    """Parse an issues section from YAML."""
    if not raw:
        return IssuesConfig()
    return IssuesConfig(
        enabled=bool(raw.get("enabled", False)),
        label=raw.get("label", "minions"),
    )


def _parse_services(raw: dict) -> dict[str, ServiceTarget]:
    """Parse services section from YAML."""
    if not raw:
        return {}
    services = {}
    for name, svc in raw.items():
        if svc is None:
            continue
        services[name] = ServiceTarget(
            name=name,
            project_id=svc.get("project_id", ""),
            git_provider=svc.get("git_provider", "gitlab"),
            gitlab_url=svc.get("gitlab_url", ""),
            repo_path=svc.get("repo_path", ""),
            clone_url=svc.get("clone_url", ""),
            ci_type=svc.get("ci_type", ""),
            deploy_target=svc.get("deploy_target", ""),
            language=svc.get("language", ""),
            framework=svc.get("framework", ""),
            test_command=svc.get("test_command", ""),
            lint_command=svc.get("lint_command", ""),
            default_branch=svc.get("default_branch", "main"),
        )
    return services


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
            engineer_profile=_parse_agent_profile(proj.get("engineer_profile", {})),
            deploy_profile=_parse_agent_profile(proj.get("deploy_profile", {})),
            services=_parse_services(proj.get("services", {})),
            issues=_parse_issues_config(proj.get("issues", {})),
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
