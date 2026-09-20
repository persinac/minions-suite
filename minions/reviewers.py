"""Which expert reviewers a PR wakes.

Ported from the swarm orchestrator's pre-scan. Every specialist is now
signal-gated, including `api` and `backend-architecture` — conditionality is
the cost control, and it used to stop at three of five, now six.
`backend-architecture` is still the broad default (it fires on almost
anything that isn't purely frontend/UI), but it is no longer unconditional,
and neither is `api`.

That changed after job 3945783f (2026-08-31): on a pure-C firmware diff,
`api` correctly self-abstained ("no public-surface changes") but still cost a
full agent run to say so, and `backend-architecture` had no abstention path
at all — it approved on "no architectural footprint, no loops, no scaling
implications", which is true and also not a review of anything. Neither
persona's checklist covers embedded C, so neither could have caught the
actual bug in that diff (`#ifdef DEV_PRINT` vs `#if DEV_PRINT` — the engineer
found it, not a reviewer).

First fix folded the embedded-C checklist into `backend-architecture` itself.
That was wrong: on job de255816 (the very next firmware PR), only
`backend-architecture` fired, when a firmware diff should wake it AND a
dedicated embedded specialist — the same "generalist plus specialist" shape
`dba`/`pythonista`/`frontend` already have relative to `backend-architecture`.
`firmware` (prompts/reviewers/firmware.md) now owns the embedded-specific
hazards (ISR safety, volatile correctness, buffer/pointer bounds, guard
correctness); `backend-architecture` keeps the language-agnostic
architecture lens and defers embedded specifics to it, same as it already
defers SQL to `dba` and Python idiom to `pythonista`.

Note the DBA trigger is deliberately CONTENT-based as well as path-based. A
migration is obvious from its path, but a lock-taking `ALTER TABLE` or an N+1
`session.query` inside ordinary application code is not, and those are the
findings a DBA is actually there to catch. `project_registry.infer_profile`
matches on paths alone and would miss every one of them. `api`'s trigger is
built the same way for the same reason: a FastAPI route defined in `main.py`
has no tell-tale path either. `firmware`, like `frontend` and `pythonista`,
is extension-based — C/C++ hazards don't depend on which repo the file lives
in, so gate on the language, not the path.
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
FIRMWARE = "firmware"

_ALL_SPECIALTIES = (API, BACKEND_ARCHITECTURE, FIRMWARE, PYTHONISTA, DBA, FRONTEND)

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

# Path segments that are almost never anything but a REST/RPC contract
# surface, plus schema/IDL file types that ARE the contract.
_API_PATH = re.compile(
    r"(^|/)(api|routes?|endpoints?|controllers?)(/|$)|\.proto$|openapi\.(ya?ml|json)$|swagger\.(ya?ml|json)$",
    re.IGNORECASE,
)

# Content signals for contract surface that doesn't live under a tell-tale
# path — a FastAPI route defined in main.py, e.g. Deliberately framework-name
# specific rather than a generic "def handler" heuristic: the false-positive
# cost (api fires on something that isn't a contract) is a wasted agent run,
# same as the api-always-on baseline this replaces, so it's fine to extend
# this list as new frameworks show up in the fleet rather than guess broadly
# up front.
_API_SIGNALS = (
    "apirouter(",
    "include_router(",
    "@app.get(",
    "@app.post(",
    "@app.put(",
    "@app.delete(",
    "@app.patch(",
    "@router.get(",
    "@router.post(",
    "@router.put(",
    "@router.delete(",
    "@router.patch(",
    "@app.route(",  # flask
    "response_model=",
    "from fastapi import",
    "graphene.objecttype",
    "strawberry.type",
)

_FIRMWARE_EXT = (".c", ".h", ".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx", ".ino", ".s")


def _is_firmware(path: str) -> bool:
    return path.lower().endswith(_FIRMWARE_EXT)


def _is_frontend(path: str) -> bool:
    lowered = path.lower()
    # .d.ts is a type declaration — no component logic to review.
    if lowered.endswith(".d.ts"):
        return False
    return lowered.endswith(_FRONTEND_EXT)


def _is_pure_frontend(changed_files: list[str]) -> bool:
    """True only if every changed file is frontend/UI — and there's at least one.

    This is `backend-architecture`'s negative gate: it fires on anything that
    ISN'T this. A single non-frontend file alongside ten frontend ones is
    enough to wake it — "almost everything" means the bar for exclusion is
    100% frontend, not majority frontend.
    """
    return bool(changed_files) and all(_is_frontend(path) for path in changed_files)


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


def _is_api_surface(changed_files: list[str], diff: str) -> bool:
    if any(_API_PATH.search(path) for path in changed_files):
        return True

    if not diff:
        return False

    added = "\n".join(line[1:] for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++"))
    if not added:
        return False

    lowered = added.lower()
    return any(signal in lowered for signal in _API_SIGNALS)


def infer_specialists(changed_files: list[str], diff: str = "") -> list[str]:
    """Reviewer specialties this PR should wake, in a stable order.

    `diff` is optional: without it, `api` and the DBA fall back to path
    signals only — a weaker but never-wrong subset.

    Every specialist here is conditional. `backend-architecture` is the
    broadest gate (fires unless the diff is purely frontend/UI) but it is a
    gate, not a default — a diff with zero changed files wakes nobody, which
    is correct: there's nothing to review.
    """
    selected: list[str] = []

    if _is_api_surface(changed_files, diff):
        selected.append(API)

    if changed_files and not _is_pure_frontend(changed_files):
        selected.append(BACKEND_ARCHITECTURE)

    if any(_is_firmware(path) for path in changed_files):
        selected.append(FIRMWARE)

    if any(path.lower().endswith(".py") for path in changed_files):
        selected.append(PYTHONISTA)

    if _touches_database(changed_files, diff):
        selected.append(DBA)

    if any(_is_frontend(path) for path in changed_files):
        selected.append(FRONTEND)

    return selected


def skipped_specialists(selected: list[str]) -> list[str]:
    """The specialists that did not fire — for the audit line."""
    return [s for s in _ALL_SPECIALTIES if s not in selected]


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
# `backend-architecture` is the anchor: it's still the single broadest
# correctness lens, now covering everything except pure-frontend diffs rather
# than everything unconditionally. `firmware` slots in at the TOP of the
# remaining priority, ahead of everything that predates it: firmware ships to
# physical devices with no hardware-in-the-loop testing anywhere in this
# pipeline (confirmed 2026-08-31 — no self-hosted/bench runner in any of the
# three firmware repos), so a review here is the only check that will ever
# run on that code. The other four keep the relative order they already had —
# that ranking (pythonista, dba, frontend, api last) predates this change and
# isn't being revisited here.

FANOUT_ANCHOR = BACKEND_ARCHITECTURE

# Order in which a fired conditional claims the remaining slot(s).
_CONDITIONAL_PRIORITY = (FIRMWARE, PYTHONISTA, DBA, FRONTEND, API)


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


def discussing_specialists(verdicts: dict[str, str | None]) -> list[str]:
    """Specialties that answered DISCUSS, when the round is otherwise decisive.

    A discuss verdict has no forum to land in — no human is watching an
    autonomous run — so the specialists who asked for one get ONE more run
    with an instruction to commit, the same shape as missing_verdicts.

    Returns [] when anyone requested changes (a revision is happening
    regardless, and the discussion points ride along in the review text) or
    when anyone returned nothing usable (aggregation fails closed to
    request_changes there, and re-asking a discusser cannot change that).
    So, like missing_verdicts, acting on this can only ever affect the round
    where discussion is the sole thing standing between the PR and a verdict.
    """
    if not verdicts:
        return []

    resolved = {s: normalise_verdict(v) for s, v in verdicts.items()}
    if any(v == REQUEST_CHANGES for v in resolved.values()):
        return []
    if any(v is None for v in resolved.values()):
        return []
    return sorted(s for s, v in resolved.items() if v == DISCUSS)


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
