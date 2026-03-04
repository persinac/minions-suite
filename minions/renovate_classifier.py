"""Pure classification functions for Renovate bot MRs.

No I/O — all functions take plain values and return plain values.
Used by RenovateEngine to decide whether to auto-merge or escalate.
"""

import re

from .models import RiskLevel

# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def is_renovate_mr(author: str, branch: str, bot_usernames: list[str], branch_prefixes: list[str]) -> bool:
    """Return True if the MR was created by Renovate bot."""
    if author in bot_usernames:
        return True
    return any(branch.startswith(prefix) for prefix in branch_prefixes)


# ---------------------------------------------------------------------------
# Version parsing
# ---------------------------------------------------------------------------

# Common Renovate title patterns:
#   "Update dependency foo to v1.2.3"
#   "Update foo monorepo to v1.2.3"
#   "chore(deps): update foo from 1.0.0 to 2.0.0"
#   "fix(deps): update dependency @scope/pkg to ^3.0.0"
#   "Update foo to 1.2.3 (from 1.1.0)"
#   "Pin dependency foo to 1.2.3"

_TITLE_PATTERNS = [
    # "update <dep> from <ver> to <ver>"
    re.compile(r"update\s+(?:dependency\s+)?(.+?)\s+from\s+v?(\d[\w.\-]*)\s+to\s+v?(\d[\w.\-]*)", re.IGNORECASE),
    # "update <dep> to v<ver>"  (no from version — extract from branch)
    re.compile(r"update\s+(?:dependency\s+)?(.+?)\s+to\s+[v^~]?(\d[\w.\-]*)", re.IGNORECASE),
    # "pin dependency <dep> to <ver>"
    re.compile(r"pin\s+(?:dependency\s+)?(.+?)\s+to\s+v?(\d[\w.\-]*)", re.IGNORECASE),
]

# Branch patterns: "renovate/foo-1.x", "renovate/major-something"
_BRANCH_MAJOR_HINT = re.compile(r"(?:^|/)major[/-]", re.IGNORECASE)
_BRANCH_VERSION = re.compile(r"-(\d+\.\d+(?:\.\d+)?(?:[-.\w]*)?)$")


def _compare_versions(from_ver: str, to_ver: str) -> RiskLevel:
    """Determine risk level by comparing semver-ish version strings."""

    def _parts(v: str) -> list[int]:
        cleaned = re.sub(r"[^0-9.]", "", v)
        return [int(x) for x in cleaned.split(".") if x]

    f = _parts(from_ver)
    t = _parts(to_ver)

    if not f or not t:
        return RiskLevel.UNKNOWN

    # Pad to at least 3 parts
    while len(f) < 3:
        f.append(0)
    while len(t) < 3:
        t.append(0)

    if t[0] > f[0]:
        return RiskLevel.MAJOR
    if t[1] > f[1]:
        return RiskLevel.MINOR
    return RiskLevel.PATCH


def parse_version_bump(title: str, branch: str) -> tuple[str, str, str, RiskLevel]:
    """Parse dependency name, from/to versions, and risk level from MR title + branch.

    Returns (dependency_name, from_version, to_version, risk_level).
    """
    dep_name = ""
    from_ver = ""
    to_ver = ""
    risk = RiskLevel.UNKNOWN

    # Try title patterns
    for pattern in _TITLE_PATTERNS:
        m = pattern.search(title)
        if not m:
            continue
        groups = m.groups()
        dep_name = groups[0].strip()
        if len(groups) == 3:
            # "from X to Y" pattern
            from_ver = groups[1]
            to_ver = groups[2]
        elif len(groups) == 2:
            # "to Y" pattern (no from)
            to_ver = groups[1]
        break

    # Clean up dep name — remove "monorepo" suffix
    if dep_name:
        dep_name = re.sub(r"\s+monorepo$", "", dep_name, flags=re.IGNORECASE).strip()

    # Determine risk level
    if from_ver and to_ver:
        risk = _compare_versions(from_ver, to_ver)
    elif _BRANCH_MAJOR_HINT.search(branch):
        risk = RiskLevel.MAJOR
    elif to_ver:
        # Can't determine without from_ver — assume minor
        risk = RiskLevel.MINOR

    # Fall back to extracting dep name from branch
    if not dep_name and branch:
        # renovate/foo-1.2.3 → foo
        cleaned = branch.split("/")[-1] if "/" in branch else branch
        cleaned = _BRANCH_VERSION.sub("", cleaned)
        if cleaned:
            dep_name = cleaned

    return dep_name, from_ver, to_ver, risk


# ---------------------------------------------------------------------------
# Risk classification
# ---------------------------------------------------------------------------


def classify_risk(
    risk_level: RiskLevel,
    dep_name: str,
    excluded_packages: list[str],
    auto_merge_patch: bool,
    auto_merge_minor: bool,
    auto_merge_major: bool,
) -> str:
    """Decide action based on risk level and project config.

    Returns "auto_merge", "human_review", or "skip".
    """
    # Check excluded packages
    dep_lower = dep_name.lower()
    for excluded in excluded_packages:
        if excluded.lower() in dep_lower:
            return "human_review"

    if risk_level == RiskLevel.PATCH:
        if auto_merge_patch:
            return "auto_merge"
        return "human_review"

    if risk_level == RiskLevel.MINOR:
        if auto_merge_minor:
            return "auto_merge"
        return "human_review"

    if risk_level == RiskLevel.MAJOR:
        if auto_merge_major:
            return "auto_merge"
        return "human_review"

    # UNKNOWN — treat conservatively
    return "human_review"


# ---------------------------------------------------------------------------
# Final merge decision
# ---------------------------------------------------------------------------


def should_auto_merge(
    ci_status: str,
    has_conflicts: bool,
    risk_decision: str,
    require_ci_pass: bool,
) -> tuple[bool, str]:
    """Decide whether to auto-merge and provide a reason string.

    Returns (should_merge, reason).
    """
    if risk_decision != "auto_merge":
        return False, f"Risk decision: {risk_decision}"

    if has_conflicts:
        return False, "MR has merge conflicts"

    if require_ci_pass and ci_status not in ("success", ""):
        if ci_status in ("running", "pending"):
            # Pipeline still running — we'll use merge-when-pipeline-succeeds
            return True, f"CI {ci_status} — will merge when pipeline succeeds"
        return False, f"CI status is '{ci_status}' (require_ci_pass=true)"

    if ci_status == "success":
        return True, "CI passed, safe to merge"

    # CI not required and no pipeline yet
    return True, "CI not required or not yet available"
