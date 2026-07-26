"""Ticket difficulty classification via RICE, for routing a job to a model tier.

One backend_engineer run on `claude-opus-5` billed $20.57. Much of the work
minions get does not need that: adding a test that mirrors an existing one,
bumping a dependency, fixing a typo'd constant. Paying Opus rates for those is
the single largest avoidable cost in the system.

Haiku scores the four RICE factors, and the score picks the model for *every*
agent on the job. The classifier call itself is a few hundred tokens on the
cheapest model — fractions of a cent against a tier gap of 5x on both input and
output pricing, so one correct "easy" verdict pays for several hundred calls.

**Why not the classic RICE priority score.** RICE as normally used ranks work by
value: (Reach x Impact x Confidence) / Effort, where a high score is a quick
win. Routing on that directly inverts for small tickets — a typo fix scores
(1 x 0.25 x 1.0) / 0.5 = 0.5, near the bottom, and would be sent to the most
expensive model precisely because it is trivial and low-impact. Reach and Impact
measure how much a change is *worth*, not how hard it is.

So the same four factors are read for difficulty instead:

    D = Effort / Confidence

Effort is the work; dividing by confidence inflates it when the ticket itself is
vague about the approach — which is exactly when a stronger model earns its
price. Reach and Impact are kept as a stakes guard: a wide, high-impact change
is floored at the medium tier even when it looks mechanical, because being wrong
there is expensive in a way that is not measured in tokens.

Failure is non-fatal by design. Any error, unparseable answer or out-of-range
score leaves the job unclassified, and unclassified jobs use the configured
default model. The classifier can only make a job cheaper or leave it unchanged.
"""

import json
import logging

import litellm

logger = logging.getLogger(__name__)

EASY = "easy"
MEDIUM = "medium"
HARD = "hard"

VALID_DIFFICULTIES = frozenset({EASY, MEDIUM, HARD})

_SYSTEM_PROMPT = """You score software tickets on the four RICE factors, for an autonomous coding agent that will implement the ticket.

Reply with ONLY a JSON object. No prose, no code fence:
{"reach": <number>, "impact": <number>, "confidence": <number>, "effort": <number>, "reason": "<one short sentence>"}

reach      1-10.  How much of the system the change touches. 1 = a single
                  function or file. 5 = one service or module. 10 = cross-cutting,
                  many services, or a public API everyone depends on.
impact     One of 0.25, 0.5, 1, 2, 3. Consequence of getting it wrong.
                  0.25 = cosmetic. 1 = normal feature work. 3 = data loss, auth,
                  billing, migrations, anything irreversible.
confidence 0.2-1.0. How clearly the ticket specifies the approach.
                  1.0 = step-by-step, pattern already exists in the repo.
                  0.5 = goal is clear, approach is not.
                  0.2 = the ticket says the approach is unknown, or that figuring
                        out how is the bulk of the work.
effort     0.5-10. Implementation work for a competent agent.
                  0.5 = one-line change. 2 = a handful of files following an
                  existing pattern. 5 = a new component or a pattern the repo
                  does not have yet. 10 = large refactor across many files.

Score honestly. Do not inflate reach or impact for small tickets, and do not
report high confidence when the ticket admits uncertainty."""

# Difficulty = effort / confidence. Boundaries calibrated against real tickets:
#   typo fix            effort 0.5, confidence 1.0 ->  0.50  easy
#   bump a dependency   effort 1.0, confidence 0.9 ->  1.11  easy
#   add a mirrored test effort 1.5, confidence 0.9 ->  1.67  easy
#   new endpoint        effort 3.0, confidence 0.8 ->  3.75  medium
#   new test-fixture pattern (the wallet-api ticket, which really did need Opus
#                       and billed $20.57)
#                       effort 6.0, confidence 0.6 -> 10.00  hard
#   auth refactor       effort 8.0, confidence 0.4 -> 20.00  hard
#
# Set deliberately loose: the cheap tiers are preferred and a misrouted ticket
# costs one wasted run, which is still less than routing everything to Opus.
EASY_MAX = 3.0
MEDIUM_MAX = 8.0

# Reach x Impact at or above this is treated as high-stakes and floored at the
# medium tier regardless of how mechanical the change looks.
STAKES_FLOOR = 15.0

_IMPACT_VALUES = (0.25, 0.5, 1.0, 2.0, 3.0)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _nearest_impact(value: float) -> float:
    return min(_IMPACT_VALUES, key=lambda allowed: abs(allowed - value))


def score_to_difficulty(effort: float, confidence: float, reach: float, impact: float) -> tuple[str, float]:
    """Map RICE factors to a difficulty tier. Returns (difficulty, raw score)."""
    confidence = _clamp(confidence, 0.2, 1.0)
    effort = _clamp(effort, 0.5, 10.0)
    reach = _clamp(reach, 1.0, 10.0)
    impact = _nearest_impact(impact)

    score = effort / confidence

    if score <= EASY_MAX:
        difficulty = EASY
    elif score <= MEDIUM_MAX:
        difficulty = MEDIUM
    else:
        difficulty = HARD

    # Stakes guard: never send a wide, high-consequence change to the cheapest
    # tier just because the diff looks small.
    if difficulty == EASY and (reach * impact) >= STAKES_FLOOR:
        logger.info("Stakes guard: reach=%.1f x impact=%.2f >= %.0f — raising easy to medium", reach, impact, STAKES_FLOOR)
        difficulty = MEDIUM

    return difficulty, score


def _parse(raw: str) -> tuple[dict | None, str]:
    """Pull the RICE factors out of the model's reply."""
    text = (raw or "").strip()
    if text.startswith("```"):
        body = text[3:]
        text = body.split("```")[0] if "```" in body else body
        text = text.removeprefix("json").strip()

    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None, f"unparseable reply: {text[:120]}"

    if not isinstance(payload, dict):
        return None, f"expected an object, got {type(payload).__name__}"

    try:
        factors = {
            "reach": float(payload["reach"]),
            "impact": float(payload["impact"]),
            "confidence": float(payload["confidence"]),
            "effort": float(payload["effort"]),
        }
    except (KeyError, TypeError, ValueError) as e:
        return None, f"missing or non-numeric RICE factor: {e}"

    factors["reason"] = str(payload.get("reason", "")).strip()
    return factors, ""


async def classify_difficulty(spec: str, config) -> tuple[str | None, str]:
    """Classify a spec as easy/medium/hard from its RICE factors.

    Returns (difficulty, reason). difficulty is None when classification was
    disabled, failed, or produced something unusable — callers must treat that
    as "use the default model", never as an error worth failing the job over.
    """
    if not getattr(config, "classifier_enabled", False):
        return None, "classifier disabled"

    if not spec or not spec.strip():
        return None, "empty spec"

    excerpt = spec.strip()[: config.classifier_max_chars]

    try:
        response = await litellm.acompletion(
            model=config.classifier_model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": excerpt},
            ],
            max_tokens=300,
            timeout=60,
        )
    except Exception as e:
        logger.warning("Difficulty classification failed (%s) — falling back to the default model", e)
        return None, f"classifier error: {e}"

    factors, error = _parse((response.choices[0].message.content or "").strip())
    if factors is None:
        logger.warning("Difficulty classification unusable (%s) — falling back to the default model", error)
        return None, error

    difficulty, score = score_to_difficulty(
        effort=factors["effort"],
        confidence=factors["confidence"],
        reach=factors["reach"],
        impact=factors["impact"],
    )

    try:
        cost = float(litellm.completion_cost(completion_response=response) or 0.0)
    except Exception:
        cost = 0.0

    reason = (
        f"R={factors['reach']:.0f} I={factors['impact']:.2f} C={factors['confidence']:.2f} "
        f"E={factors['effort']:.1f} -> E/C={score:.2f} => {difficulty}"
        f"{' (' + factors['reason'] + ')' if factors['reason'] else ''}"
    )
    logger.info("Classified job %s [classifier cost $%.5f]", reason, cost)
    return difficulty, reason


class UnpriceableModelError(RuntimeError):
    """Raised when a configured model has no cost data and ceilings are enabled."""


def is_priceable(model: str) -> bool:
    """Whether LiteLLM can compute a cost for this model string.

    Matters because the spend ceilings are derived from litellm.completion_cost,
    which returns 0.0 for an unknown model. A model LiteLLM cannot price does not
    make agents free — it makes the $8 agent limit and the $25 job limit silently
    inert, which is strictly worse than having no limits, because the config
    still says they are on.

    Provider prefixes matter here: `moonshot/kimi-k2.6` is priced,
    `openai/kimi-k2.6` routes fine and is NOT, and bare `kimi-k2.6` does not
    route at all.
    """
    import litellm

    if not model:
        return False
    if model in litellm.model_cost:
        return True
    # LiteLLM also matches on the bare name for some providers.
    return model.split("/")[-1] in litellm.model_cost


def assert_priceable(model: str, config, role: str = "agent") -> None:
    """Refuse an unpriceable model while spend ceilings are supposed to apply.

    Allowed when both ceilings are disabled: the guard exists to protect the
    ceilings, so with none configured there is nothing to protect and a model
    LiteLLM has not catalogued yet should not be blocked.
    """
    if is_priceable(model):
        return

    ceilings_on = (getattr(config, "agent_cost_limit_usd", 0) or 0) > 0 or (getattr(config, "job_cost_limit_usd", 0) or 0) > 0

    if not ceilings_on:
        logger.warning("Model %r has no LiteLLM cost data; spend will record as $0 (ceilings are disabled)", model)
        return

    raise UnpriceableModelError(
        f"Model {model!r} for {role} has no LiteLLM cost data, so completion_cost() returns 0 "
        f"and the configured spend ceilings would never fire. Use a priced model string "
        f"(provider prefix matters: 'moonshot/kimi-k2.6' is priced, 'openai/kimi-k2.6' is not), "
        f"or set AGENT_COST_LIMIT_USD=0 and JOB_COST_LIMIT_USD=0 to accept unbounded spend deliberately."
    )


def resolve_model(config, difficulty: str | None, project_model: str = "", is_reviewer: bool = False, is_engineer: bool = False) -> str:
    """Pick the model for an agent.

    Precedence: an explicit per-project model wins (someone chose it
    deliberately), then the reviewer default, then the difficulty tier, then the
    global default.

    Reviewers get their own default because they fan out. One engineer runs per
    task; up to five specialists review its output, so reviewer cost multiplies
    against a job ceiling the engineer has already eaten into. An easy ticket
    still pulls everything down to the cheap tier — the reviewer default only
    applies from medium upward, where it would otherwise be Opus.
    """
    if project_model:
        return project_model

    tiers = {
        EASY: config.model_easy,
        MEDIUM: config.model_medium,
        HARD: config.model_hard,
    }
    tier_model = tiers.get(difficulty or "")

    if is_reviewer:
        reviewer_default = getattr(config, "model_reviewer", "")
        if reviewer_default:
            # Never spend *more* than the tier: an easy ticket keeps Haiku.
            if difficulty == EASY and tier_model:
                return tier_model
            return reviewer_default

    if is_engineer:
        # Engineers are ~79% of job cost and the workload is input-dominated
        # (3.85M in vs 53k out on the one measured job), so this is the lever
        # worth pointing at a cheaper vendor. Empty = follow the difficulty tier,
        # which keeps behaviour unchanged until someone sets it deliberately.
        engineer_default = getattr(config, "model_engineer", "")
        if engineer_default:
            return engineer_default

    return tier_model or config.model
