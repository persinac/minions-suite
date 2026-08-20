"""Which expert reviewers a PR wakes.

Ported from the swarm orchestrator's pre-scan. Two reviewers always run; three
fire only on signals in the diff, so a Python-only test PR wakes three
specialists rather than five. That conditionality is the cost control — each
reviewer is a full agent run.

Note the DBA trigger is deliberately CONTENT-based as well as path-based. A
migration is obvious from its path, but a lock-taking `ALTER TABLE` or an N+1
`session.query` inside ordinary application code is not, and those are the
findings a DBA is actually there to catch. `project_registry.infer_profile`
matches on paths alone and would miss every one of them.
"""

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

API = "api"
BACKEND_ARCHITECTURE = "backend-architecture"
DBA = "dba"
PYTHONISTA = "pythonista"
FRONTEND = "frontend"

# Fire on every PR: API surface and architecture are always in scope.
ALWAYS_RUN = (API, BACKEND_ARCHITECTURE)

_DB_PATH = re.compile(r"(\.sql$|/migrations?/|^migrations?/|/migrate/|/alembic/|/versions/)", re.IGNORECASE)

# Standalone tokens, not substrings: "updated" must not trigger on \bUPDATE\b.
_DB_SQL_TOKENS = re.compile(
    r"\b(SELECT\b.+?\bFROM|INSERT\s+INTO|UPDATE\b.+?\bSET|CREATE\s+(TABLE|INDEX)|ALTER\s+TABLE|DROP\s+(TABLE|INDEX|COLUMN))\b",
    re.IGNORECASE | re.DOTALL,
)

_DB_ORM_SIGNALS = (
    ".objects.",
    "session.query",
    "db.execute",
    "cursor.execute",
    "sqlalchemy.text(",
    ".raw_sql",
    ".exec_driver_sql",
    "op.add_column",
    "op.create_table",
)

_FRONTEND_EXT = (".tsx", ".jsx", ".ts", ".js")


def _is_frontend(path: str) -> bool:
    lowered = path.lower()
    # .d.ts is a type declaration — no component logic to review.
    if lowered.endswith(".d.ts"):
        return False
    return lowered.endswith(_FRONTEND_EXT)


def _touches_database(changed_files: list[str], diff: str) -> bool:
    if any(_DB_PATH.search(path) for path in changed_files):
        return True

    if not diff:
        return False

    # Only consider ADDED lines. A diff's context and removed lines carry the
    # old code, and flagging a DBA onto a PR that *deletes* the last raw query
    # is a wasted agent run.
    added = "\n".join(line[1:] for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++"))
    if not added:
        return False

    if any(signal in added for signal in _DB_ORM_SIGNALS):
        return True
    return bool(_DB_SQL_TOKENS.search(added))


def infer_specialists(changed_files: list[str], diff: str = "") -> list[str]:
    """Reviewer specialties this PR should wake, in a stable order.

    `diff` is optional: without it the DBA falls back to path signals only, which
    is a weaker but never-wrong subset.
    """
    selected = list(ALWAYS_RUN)

    if any(path.lower().endswith(".py") for path in changed_files):
        selected.append(PYTHONISTA)

    if _touches_database(changed_files, diff):
        selected.append(DBA)

    if any(_is_frontend(path) for path in changed_files):
        selected.append(FRONTEND)

    return selected


def skipped_specialists(selected: list[str]) -> list[str]:
    """The conditional reviewers that did not fire — for the audit line."""
    conditional = (PYTHONISTA, DBA, FRONTEND)
    return [s for s in conditional if s not in selected]


# --- Fan-out cap ------------------------------------------------------------
#
# Measured 2026-08-20 over 20 PRs with 2+ verdicts: at the configured width
# (<=3), 10 of 12 PRs were unanimous and only 16.7% split -- the marginal
# reviewer rarely changed the outcome, for $6.82 of redundant spend.
#
# NOT measured, and the reason this is a knob rather than a new default buried
# in infer_specialists: a narrower gate is invisible until something bad merges.
# The saving shows up immediately; the miss does not show up at all.
#
# Priority is "signal wins": a specialist fired because the diff contained
# evidence for it, which makes it more informative than a generalist that runs
# unconditionally. So `api` -- which fires on every PR regardless of content --
# is the one that yields. `backend-architecture` is the anchor because it is the
# only broad correctness lens left once `api` can drop.

FANOUT_ANCHOR = BACKEND_ARCHITECTURE

# Order in which a fired conditional claims the remaining slot(s).
_CONDITIONAL_PRIORITY = (PYTHONISTA, DBA, FRONTEND)


def cap_specialists(selected: list[str], limit: int) -> list[str]:
    """Narrow `selected` to at most `limit` reviewers, keeping the informative ones.

    `limit <= 0` means uncapped, matching the convention used by the cost
    ceilings. When the cap does not bind, `selected` is returned unchanged --
    including its order -- so raising the limit restores byte-identical
    behaviour rather than a reordered near-miss.
    """
    if limit <= 0 or len(selected) <= limit:
        return selected

    priority = [FANOUT_ANCHOR]
    priority += [s for s in _CONDITIONAL_PRIORITY if s in selected]
    priority.append(API)

    keep: list[str] = []
    for specialty in priority:
        if len(keep) >= limit:
            break
        if specialty in selected and specialty not in keep:
            keep.append(specialty)

    # Return in the original stable order: priority decides membership, not
    # ordering, and a stable order keeps the audit line diffable across runs.
    return [s for s in selected if s in keep]


def capped_specialists(selected: list[str], kept: list[str]) -> list[str]:
    """Reviewers that fired but were dropped by the cap — for the audit line.

    Distinct from `skipped_specialists`, which reports the ones that never fired
    at all. Conflating the two would hide the gate narrowing behind what looks
    like an ordinary quiet diff.
    """
    return [s for s in selected if s not in kept]


# --- Verdict aggregation ----------------------------------------------------
#
# Per the swarm orchestrator: any REQUEST_CHANGES wins, then any DISCUSS, else
# APPROVE. N/A abstains — a specialist that found nothing in scope is not a vote
# for merging.

APPROVE = "approve"
REQUEST_CHANGES = "request_changes"
DISCUSS = "discuss"
NOT_APPLICABLE = "n/a"

_VERDICT_ALIASES = {
    "approve": APPROVE,
    "approved": APPROVE,
    "request_changes": REQUEST_CHANGES,
    "request changes": REQUEST_CHANGES,
    "changes_requested": REQUEST_CHANGES,
    "discuss": DISCUSS,
    "needs_discussion": DISCUSS,
    "n/a": NOT_APPLICABLE,
    "na": NOT_APPLICABLE,
    "not_applicable": NOT_APPLICABLE,
}


def normalise_verdict(raw: str | None) -> str | None:
    """Map a reviewer's verdict onto the canonical set. None if unusable."""
    if not raw:
        return None
    return _VERDICT_ALIASES.get(str(raw).strip().lower().replace("-", "_"))


def missing_verdicts(verdicts: dict[str, str | None]) -> list[str]:
    """Specialties that returned nothing usable, when NOTHING actually objected.

    aggregate_verdicts fails closed on an absent verdict, which is right —
    silence must never count as assent. But the caller then cannot tell "a
    reviewer objected" from "a reviewer did not answer", and those want opposite
    handling: the first needs a revision, the second needs that reviewer run
    again.

    Collapsing them cost two merges. Job 33c89d9b: api and backend-architecture
    both APPROVED, pythonista returned nothing, and the forced revision round
    came back with the opposite verdict on identical code. Job 2b63f1b6: two
    approvals, zero findings, and a herder was asked to revise a PR nobody had
    objected to.

    Returns [] when there is a real objection, so a genuine block always goes to
    revision and this can only ever short-circuit the no-objection case.
    """
    if not verdicts:
        return []

    resolved = {s: normalise_verdict(v) for s, v in verdicts.items()}
    if any(v == REQUEST_CHANGES for v in resolved.values()):
        return []
    return sorted(s for s, v in resolved.items() if v is None)


def aggregate_verdicts(verdicts: dict[str, str | None]) -> tuple[str, str]:
    """Collapse per-specialty verdicts into one decision.

    Takes {specialty: verdict}. Returns (verdict, human-readable reason).

    Fails CLOSED on a missing or unparseable verdict, matching the single-reviewer
    path: a specialist that crashed, timed out or hit its cost ceiling has not
    approved anything, and treating silence as assent is what let unreviewed code
    merge once already.

    Any REQUEST_CHANGES blocks outright — each specialist speaks only about its
    own domain, so a DBA objection is not outvoted by four approvals in areas it
    never looked at.
    """
    if not verdicts:
        return REQUEST_CHANGES, "no reviewers ran"

    missing = sorted(s for s, v in verdicts.items() if normalise_verdict(v) is None)
    if missing:
        return REQUEST_CHANGES, f"no usable verdict from: {', '.join(missing)}"

    resolved = {s: normalise_verdict(v) for s, v in verdicts.items()}

    blocking = sorted(s for s, v in resolved.items() if v == REQUEST_CHANGES)
    if blocking:
        return REQUEST_CHANGES, f"changes requested by: {', '.join(blocking)}"

    discussing = sorted(s for s, v in resolved.items() if v == DISCUSS)
    if discussing:
        return DISCUSS, f"discussion requested by: {', '.join(discussing)}"

    approving = sorted(s for s, v in resolved.items() if v == APPROVE)
    if not approving:
        # Everyone returned N/A. Nobody actually reviewed anything.
        return REQUEST_CHANGES, "every reviewer returned N/A — nothing was actually reviewed"

    abstained = sorted(s for s, v in resolved.items() if v == NOT_APPLICABLE)
    reason = f"approved by: {', '.join(approving)}"
    if abstained:
        reason += f" (n/a: {', '.join(abstained)})"
    return APPROVE, reason


# --- Persona loading --------------------------------------------------------

_PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts" / "reviewers"


def load_persona(specialty: str) -> str:
    """The reviewer persona for a specialty, or "" if it has no prompt file.

    Returning "" rather than raising is deliberate: a missing persona should
    degrade that specialist to a generic review, not take down the whole fan-out
    and lose the four reviewers that do have prompts.
    """
    path = _PROMPT_DIR / f"{specialty}.md"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        logger.warning("No reviewer persona at %s — %s will review without a lens", path, specialty)
        return ""
