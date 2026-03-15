"""Controlled vocabulary and tag normalization for agent memory."""

# Domain tags — what area of the system
DOMAIN_TAGS = frozenset(
    {
        "auth",
        "authentication",
        "authorization",
        "security",
        "api",
        "endpoint",
        "routing",
        "middleware",
        "database",
        "migration",
        "schema",
        "query",
        "frontend",
        "ui",
        "component",
        "styling",
        "backend",
        "service",
        "worker",
        "queue",
        "infra",
        "infrastructure",
        "deploy",
        "ci",
        "cd",
        "testing",
        "test",
        "coverage",
        "fixture",
        "config",
        "configuration",
        "env",
        "settings",
        "logging",
        "monitoring",
        "observability",
        "metrics",
        "performance",
        "optimization",
        "caching",
        "error",
        "exception",
        "error-handling",
        "documentation",
        "docs",
    }
)

# Action tags — what kind of change or observation
ACTION_TAGS = frozenset(
    {
        "pattern",
        "convention",
        "standard",
        "decision",
        "choice",
        "trade-off",
        "bug",
        "fix",
        "regression",
        "workaround",
        "refactor",
        "cleanup",
        "debt",
        "feature",
        "enhancement",
        "new",
        "review",
        "feedback",
        "suggestion",
        "risk",
        "concern",
        "warning",
        "dependency",
        "upgrade",
        "breaking-change",
    }
)

# Outcome tags — what happened
OUTCOME_TAGS = frozenset(
    {
        "approved",
        "rejected",
        "deferred",
        "resolved",
        "unresolved",
        "wont-fix",
        "merged",
        "reverted",
        "deployed",
    }
)

CONTROLLED_TAGS = DOMAIN_TAGS | ACTION_TAGS | OUTCOME_TAGS

# Related-tag map for suggesting extensions
_TAG_RELATIONS: dict[str, list[str]] = {
    "auth": ["security", "authentication"],
    "authentication": ["auth", "security"],
    "authorization": ["auth", "security"],
    "api": ["endpoint", "routing"],
    "endpoint": ["api", "routing"],
    "database": ["migration", "schema", "query"],
    "migration": ["database", "schema"],
    "schema": ["database", "migration"],
    "frontend": ["ui", "component"],
    "ui": ["frontend", "component"],
    "backend": ["service", "api"],
    "infra": ["infrastructure", "deploy"],
    "infrastructure": ["infra", "deploy"],
    "deploy": ["infra", "ci", "cd"],
    "testing": ["test", "coverage"],
    "test": ["testing", "coverage"],
    "bug": ["fix", "regression"],
    "fix": ["bug", "resolved"],
    "refactor": ["cleanup", "debt"],
    "feature": ["enhancement", "new"],
    "review": ["feedback", "suggestion"],
    "risk": ["concern", "warning"],
}


def normalize_tags(raw: list[str]) -> list[str]:
    """Lowercase, strip whitespace, deduplicate tags. Preserves order."""
    seen: set[str] = set()
    result: list[str] = []
    for tag in raw:
        normalized = tag.strip().lower()
        if not normalized:
            continue
        if normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def suggest_extensions(raw: list[str]) -> list[str]:
    """Suggest related tags from the controlled vocabulary based on input tags."""
    normalized = {t.strip().lower() for t in raw if t.strip()}
    suggestions: list[str] = []
    seen = set(normalized)
    for tag in normalized:
        for related in _TAG_RELATIONS.get(tag, []):
            if related not in seen:
                seen.add(related)
                suggestions.append(related)
    return suggestions
