"""Jev-backed difficulty scoring; see openspec/changes/typesafe-classifier/design.md."""

import logging

from .classifier import EASY, HARD, MEDIUM

logger = logging.getLogger(__name__)

INPUT_USD_PER_MTOK = 0.042

EFFORT_CRITERIA = [
    "A one-line change: a typo, a renamed constant, a dependency version bump",
    "A handful of files, following a pattern that already exists in the codebase",
    "A new component, or a pattern the codebase does not have yet",
    "A large refactor touching many files, or several services at once",
]

CLARITY_CRITERIA = [
    "The ticket says the approach is unknown, or working out how is the bulk of the job",
    "The goal is clear but the approach is not specified",
    "Step-by-step, or it names a pattern that already exists in the codebase",
]

BLAST_RADIUS_CRITERIA = [
    "A single function or a single file",
    "One service or one module",
    "Cross-cutting: many services, or a public API that other systems depend on",
]

CONSEQUENCE_CRITERIA = [
    "Cosmetic: no impact on functionality",
    "Normal feature work: a bug, caught in review or by tests",
    "Irreversible: data loss, authentication, billing, or a schema migration",
]

QUESTION_NAMES = ("effort", "clarity", "blast_radius", "consequence")

# Thresholds on expectations, NOT `effort / clarity`: jev-1.13's score levels are
# "weak in numerical calibration" per its jaggedness page, and a denominator whose
# floor is 0.2 turns one notch of error into a two-tier swing. UNCALIBRATED as yet.
HARD_EFFORT = 0.67
HARD_EFFORT_IF_VAGUE = 0.45
VAGUE_CLARITY = 0.25
EASY_EFFORT = 0.25
EASY_CLARITY = 0.60
STAKES_BLAST = 0.5
STAKES_CONSEQUENCE = 0.5


def _normalize(score: float, criteria: list[str]) -> float:
    """Map a level float onto [0, 1]; scales of differing length are not comparable raw."""
    top = len(criteria) - 1
    if top <= 0:
        return 0.0
    return max(0.0, min(1.0, score / top))


def levels_to_difficulty(effort: float, clarity: float, blast_radius: float, consequence: float) -> tuple[str, dict]:
    """Map four raw Jev level scores to a tier. Takes raw scores; normalizes internally.

    Returns (difficulty, normalized detail).
    """
    e = _normalize(effort, EFFORT_CRITERIA)
    c = _normalize(clarity, CLARITY_CRITERIA)
    b = _normalize(blast_radius, BLAST_RADIUS_CRITERIA)
    q = _normalize(consequence, CONSEQUENCE_CRITERIA)

    if e >= HARD_EFFORT or (e >= HARD_EFFORT_IF_VAGUE and c <= VAGUE_CLARITY):
        difficulty = HARD
    elif e <= EASY_EFFORT and c >= EASY_CLARITY:
        difficulty = EASY
    else:
        difficulty = MEDIUM

    stakes_applied = False
    if difficulty == EASY and b >= STAKES_BLAST and q >= STAKES_CONSEQUENCE:
        logger.info("Stakes guard: blast=%.2f consequence=%.2f — raising easy to medium", b, q)
        difficulty = MEDIUM
        stakes_applied = True

    detail = {
        "effort_n": round(e, 3),
        "clarity_n": round(c, 3),
        "blast_n": round(b, 3),
        "consequence_n": round(q, 3),
        "stakes_guard": stakes_applied,
    }
    return difficulty, detail


def jev_cost_usd(usage) -> float:
    """Cost of one system_one call. Input tokens only; Usage.input_tokens may be None."""
    if usage is None:
        return 0.0
    tokens = getattr(usage, "input_tokens", None)
    if not tokens:
        return 0.0
    return (float(tokens) / 1_000_000.0) * INPUT_USD_PER_MTOK


def build_questions() -> dict:
    """The four questions. Built lazily so importing this module never requires typesafe-sdk."""
    from typesafe_sdk import Score

    return {
        "effort": Score(
            instructions="How much implementation work is this ticket for a competent autonomous coding agent?",
            criteria=EFFORT_CRITERIA,
        ),
        "clarity": Score(
            instructions="How clearly does this ticket specify the approach to take?",
            criteria=CLARITY_CRITERIA,
        ),
        "blast_radius": Score(
            instructions="How much of the system does this change touch?",
            criteria=BLAST_RADIUS_CRITERIA,
        ),
        "consequence": Score(
            instructions="How bad is it if this change is implemented incorrectly?",
            criteria=CONSEQUENCE_CRITERIA,
        ),
    }


async def _close_quietly(client) -> None:
    closer = getattr(client, "close", None)
    if closer is None:
        return
    try:
        await closer()
    except Exception:
        logger.debug("Ignoring error while closing the Jev client", exc_info=True)


async def score_difficulty_jev(spec: str, config, client=None) -> tuple[str | None, str, dict]:
    """Classify a spec via Jev.

    Returns (difficulty, reason, telemetry). `difficulty` is None whenever the
    answer must not be acted on — no key, dependency missing, API error, or
    confidence below the floor — and callers must treat None as "use the default
    tier", never as an error worth failing a job over.

    `client` is injectable so tests need neither a real client nor a key.
    """
    if not spec or not spec.strip():
        return None, "empty spec", {}

    api_key = getattr(config, "typesafe_api_key", "") or ""
    if client is None and not api_key:
        return None, "no TYPESAFE_API_KEY configured", {}

    excerpt = spec.strip()[: config.classifier_max_chars]

    owns_client = client is None
    try:
        if owns_client:
            from typesafe_sdk import AsyncTypeSafeClient

            client = AsyncTypeSafeClient(
                api_key=api_key,
                model=getattr(config, "typesafe_model", "") or None,
                timeout=float(getattr(config, "typesafe_timeout", 30.0)),
            )

        response = await client.system_one(excerpt, build_questions())
    except ImportError as e:
        logger.warning("typesafe-sdk is not installed (%s) — Jev classification skipped", e)
        return None, f"typesafe-sdk missing: {e}", {}
    except Exception as e:
        logger.warning("Jev classification failed (%s) — falling back", e)
        return None, f"jev error: {type(e).__name__}: {e}", {}
    finally:
        if owns_client and client is not None:
            await _close_quietly(client)

    try:
        answers = response.answers
        raw = {name: answers[name] for name in QUESTION_NAMES}
    except (AttributeError, KeyError, TypeError) as e:
        logger.warning("Jev response missing an expected answer (%s) — falling back", e)
        return None, f"jev response incomplete: {e}", {}

    cost = jev_cost_usd(getattr(response, "usage", None))
    confidences = {name: float(a.confidence) for name, a in raw.items()}
    weakest_name = min(confidences, key=lambda k: confidences[k])
    weakest = confidences[weakest_name]

    difficulty, detail = levels_to_difficulty(
        effort=float(raw["effort"].score),
        clarity=float(raw["clarity"].score),
        blast_radius=float(raw["blast_radius"].score),
        consequence=float(raw["consequence"].score),
    )

    telemetry = {
        "scores": {name: round(float(a.score), 3) for name, a in raw.items()},
        "confidences": {name: round(v, 3) for name, v in confidences.items()},
        "probabilities": {name: {int(k): round(float(v), 4) for k, v in a.probabilities.items()} for name, a in raw.items()},
        "normalized": detail,
        "difficulty": difficulty,
        "cost_usd": round(cost, 8),
        "model": getattr(response, "model", ""),
        "min_confidence": round(weakest, 3),
        "min_confidence_question": weakest_name,
    }

    floor = float(getattr(config, "classifier_min_confidence", 0.5))
    if weakest < floor:
        reason = f"jev confidence {weakest:.2f} on '{weakest_name}' below floor {floor:.2f} => unclassified"
        logger.info("%s [jev cost $%.6f]", reason, cost)
        telemetry["gated"] = True
        telemetry["difficulty"] = None
        return None, reason, telemetry

    telemetry["gated"] = False
    reason = (
        f"jev E={raw['effort'].score:.2f}/{len(EFFORT_CRITERIA) - 1} "
        f"C={raw['clarity'].score:.2f}/{len(CLARITY_CRITERIA) - 1} "
        f"B={raw['blast_radius'].score:.2f}/{len(BLAST_RADIUS_CRITERIA) - 1} "
        f"Q={raw['consequence'].score:.2f}/{len(CONSEQUENCE_CRITERIA) - 1} "
        f"-> e={detail['effort_n']:.2f} c={detail['clarity_n']:.2f} => {difficulty} "
        f"(min_conf {weakest:.2f} on {weakest_name})"
    )
    logger.info("Classified %s [jev cost $%.6f]", reason, cost)
    return difficulty, reason, telemetry
