# Design: Jev-backed difficulty classification

All vendor behaviour below is cited from `docs.typesafe.ai` as read on 2026-09-21.
Nothing here has been run against the live API — no `TYPESAFE_API_KEY` was present in
this environment. Every latency, accuracy, and threshold claim is therefore **documented
or assumed, not measured**, and each is tagged.

## 1. What the vendor gives us

| Thing | Value | Source |
|---|---|---|
| Endpoint | `POST /v1/systemone` | `models.md` |
| Model | `jev-1.13.0`, aliases `jev-latest` / `jev-preview` | `models.md` |
| Context | 64k total (state + all questions); 32k for state + longest question | `models.md` |
| Price | $0.042/Mtok **input only**, output free | `models.md` |
| Rate limits | 250k tok/s, 1200 req/min; `429` + `retry-after`, SDK retries | `models.md` |
| Question types | `Choice`, `Score`, `Noul` — mixed freely in one request | `primitives.md` |
| Python SDK | `typesafe_sdk`, `TypeSafeClient` / `AsyncTypeSafeClient`, `system_one(state, questions)` | `sdk/python/usage.md` |
| Auth | `TYPESAFE_API_KEY` from env, never passed in code | `sdk/python/usage.md` |
| Latency | *no numeric figure published* — only "adding questions barely changes the response time" | `introduction.md` |

`Score` specifics that drive the design (`primitives/score.md`):

- You do **not** give a numeric range. You give `criteria`: an ordered list of level
  descriptions, bottom to top. Level number = array index, starting at 0.
- At least 2 levels, **at most 10**.
- The returned `score` is a float in `[0, len(criteria)-1]`, computed as the
  probability-weighted mean of level indices. It can land between levels.
- Response carries `score`, `probabilities` (per level, summing to 1), `legend`, and
  `confidence` in `[0, 1]` derived from the *shape* of the distribution.
- To combine scores from scales of different lengths, **normalize by dividing by
  `len(criteria) - 1`** — the docs say this explicitly.
- Distinct distributions can produce identical scores: a `1.0` may be all mass on level 1,
  or an even split between 0 and 2. Read `probabilities` alongside `score`.

## 2. The constraint that reshapes the design

From `model-jaggedness/jev-1.13.md`, §2 "Math and numbers":

> Score calibration — don't reconstruct magnitudes from scores. Threshold checks via
> expectation are fine, but "jev-1.13's score levels are weak in numerical calibration,"
> and interpolating between adjacent levels to recover an exact number won't work.

And, flatly: *"Jev is not a calculator. Mathematical logic belongs in code."*

Our current formula is:

```
D = effort / confidence          # effort 0.5-10, confidence 0.2-1.0
easy if D <= 3.0, medium if D <= 8.0, else hard
```

The obvious port — four `Score` questions, then interpolate each level float back onto our
numeric anchors (`effort` levels → `[0.5, 2, 5, 10]`) and divide — is **precisely the
operation the vendor says does not work**, and it is worse than it looks:

- It reconstructs magnitudes from levels (forbidden outright).
- It then *divides* by the reconstructed `confidence`, whose legal range bottoms out at
  0.2. Division by a small, poorly-calibrated denominator amplifies its error. With
  `effort = 2`: `C = 0.2 → D = 10.0` (hard), `C = 0.3 → D = 6.7` (medium). A single
  notch of miscalibration in the denominator moves the verdict two tiers.

`score_to_difficulty()` is a well-tested pure function and stays exactly as it is for the
LiteLLM path. The Jev path needs a different recombination, not a different input to the
same one.

## 3. Questions

Four questions, one request. Each is a single atomic judgment — the docs warn against
"bundling multiple judgments into one question", and `reach × impact` is two.

Criteria are lifted from the anchors already written into `_SYSTEM_PROMPT`, which were
always level descriptions in prose form. Note §7 of the jaggedness page: inverted mappings
degrade accuracy, so every scale below ascends in the direction its name implies.

```python
QUESTIONS = {
    # 4 levels -> score 0.0-3.0
    "effort": Score(
        instructions="How much implementation work is this ticket for a competent autonomous coding agent?",
        criteria=[
            "A one-line change: a typo, a renamed constant, a dependency version bump",
            "A handful of files, following a pattern that already exists in the codebase",
            "A new component, or a pattern the codebase does not have yet",
            "A large refactor touching many files, or several services at once",
        ],
    ),
    # 3 levels -> score 0.0-2.0.  ASCENDING = clearer.  This is the RICE
    # `confidence` factor renamed: it scores the TICKET's clarity, and calling it
    # `confidence` alongside the vendor's own answer-confidence field invites
    # exactly the conflation this design has to keep apart.
    "clarity": Score(
        instructions="How clearly does this ticket specify the approach to take?",
        criteria=[
            "The ticket says the approach is unknown, or working out how is the bulk of the job",
            "The goal is clear but the approach is not specified",
            "Step-by-step, or it names a pattern that already exists in the codebase",
        ],
    ),
    # 3 levels -> score 0.0-2.0.  The RICE `reach` factor.
    "blast_radius": Score(
        instructions="How much of the system does this change touch?",
        criteria=[
            "A single function or a single file",
            "One service or one module",
            "Cross-cutting: many services, or a public API that other systems depend on",
        ],
    ),
    # 3 levels -> score 0.0-2.0.  The RICE `impact` factor, as ordered levels
    # rather than the multiplier set {0.25, 0.5, 1, 2, 3} -- a multiplier is a
    # magnitude, and magnitudes are what we are no longer reconstructing.
    "consequence": Score(
        instructions="How bad is it if this change is implemented incorrectly?",
        criteria=[
            "Cosmetic: no impact on functionality",
            "Normal feature work: a bug, caught in review or by tests",
            "Irreversible: data loss, authentication, billing, or a schema migration",
        ],
    ),
}
```

## 4. Recombination — thresholds, not arithmetic

Normalize each score to `[0, 1]` by dividing by `len(criteria) - 1` (per §1), then apply
threshold checks on those expectations. No magnitude is reconstructed and nothing is
divided by a model output.

```python
e = effort.score       / 3.0   # work
c = clarity.score      / 2.0   # how well specified
b = blast_radius.score / 2.0   # how wide
q = consequence.score  / 2.0   # how costly to get wrong

# Difficulty rises with effort and falls with clarity.
if e >= 0.67 or (e >= 0.45 and c <= 0.25):
    difficulty = HARD
elif e <= 0.25 and c >= 0.60:
    difficulty = EASY
else:
    difficulty = MEDIUM

# Stakes guard, preserved in ordinal form. The old rule was
# `reach * impact >= 15`, which needs both factors high (5x3, 8x2) -- an AND,
# expressed as a product. Keep it an AND and skip the product.
if difficulty == EASY and b >= 0.5 and q >= 0.5:
    difficulty = MEDIUM
```

> ⚠️ **Those six numbers are guesses.** `EASY_MAX = 3.0` and `MEDIUM_MAX = 8.0` earned
> their values from the calibration table in `classifier.py`'s docstring — six real
> tickets with known outcomes, including the wallet-api ticket that genuinely needed Opus
> and billed $20.57. The thresholds above have no such backing and **must not ship
> un-calibrated**. §5 is how they get calibrated.

## 5. How we would know it works

Production cannot A/B this. Routing is deterministic, so the cheap tier and the easy
tickets are the same set by construction — un-stratified comparison measures ticket mix.
`task e2e:matrix` does not help either: it covers the analyst and arbiter only, not the
classifier.

**We already own a labelled replay corpus and it is better than expected.**
`minions/engine/dev.py:647` records, for every classified job:

```python
await engine.db.record_event(job.id, "difficulty_classified", "classifier", reason)
```

…where `reason` is the fully-rendered RICE string:

```
R=5 I=1.00 C=0.60 E=6.0 -> E/C=10.00 => hard (new test-fixture pattern)
```

So each past job yields **the raw ticket** (`jobs.original_spec`, preserved on first
refine via `COALESCE`), **all four factors**, **the tier chosen**, and — joined through
the effectiveness queries — **what actually happened**: realized cost, `ceiling_hits`,
revision rounds, `real_failed`.

That is spec → factors → tier → outcome. A replay harness can score the same specs through
Jev and compare tiers against both the Haiku verdict *and* the outcome, for the price of
input tokens on a few hundred short specs — call it well under a dollar at $0.042/Mtok.

`scripts/classifier_corpus.sql` extracts it. The query is **VERIFIED against the real
schema but not against real data**: the three relevant migrations' up-sections were applied
to a scratch database in the disposable `minion-test-pg` container, and the query was run
over three synthetic rows whose reason strings came from calling the actual
`classify_difficulty()` with only `litellm.acompletion` faked. That checked the regexes
against the format the code really emits — including the trap that `C=` appears twice per
string (`C=0.60` and `E/C=10.00`) — rather than against a hand-written fixture that could
drift from it. See the file header for what would have failed.

**Corpus size is still unknown.** Real rows live in the deployed database, which needs the
`kubectl exec` path from `docs/operator-setup.md`. If N turns out small, §6 fills it
prospectively.

Calibration procedure:

1. Run the corpus query; confirm N is large enough to be worth fitting (assume ≥50).
2. Replay every spec through Jev; keep all four scores, probabilities, and confidences.
3. Fit the six thresholds to maximize agreement with *outcomes*, not with Haiku. Haiku is
   the incumbent, not the ground truth — where they disagree, the realized cost and
   ceiling hits decide.
4. Report the confusion matrix against outcome, and the disagreement rate against Haiku.

**What would make this come out negative:** if Jev's `effort` score does not separate the
tickets that came in under the easy tier from the ones that hit the ceiling, there is no
threshold that recovers the current behaviour and the answer is to keep Haiku. A
disagreement rate that is high *and* not explained by outcomes is the kill signal.

## 6. Rollout

`shadow` mode is the first deployment and it is free of behavioural risk:

- Both backends run. LiteLLM's verdict is returned and used, exactly as today.
- Jev's verdict, all four scores, probabilities, confidences, and computed cost are written
  as a `difficulty_shadow` event.
- Any Jev failure is swallowed and logged. Shadow mode cannot fail a job, and cannot
  change which model a job gets.

That builds the corpus prospectively while §5 mines it retrospectively, and it measures
real latency — which the docs never quantify — before anything depends on it.

Promotion to `CLASSIFIER_BACKEND=typesafe` waits on the calibration in §5.

## 7. Things that will bite

1. **Cost must be computed by hand.** `litellm.completion_cost()` returns `0.0` for a
   non-LiteLLM provider. Read `usage` off the response and apply $0.042/Mtok on input
   only. This does *not* endanger the spend ceilings (see `proposal.md`, Impact), but a
   silently-$0 classifier line in the effectiveness tables would misreport.
2. **Context rot on the state.** Jaggedness §5: "Accuracy falls as the state grows with
   content unrelated to the decision." We currently send `spec[:6000]` — a blunt truncation
   of ticket prose. Jev is more sensitive to irrelevant state than a chat model, so
   `CLASSIFIER_MAX_CHARS` may need to come *down*, and a tighter excerpt may beat a longer
   one. Worth an explicit sweep during calibration.
3. **Tickets argue for their own classification.** Jaggedness §6: state is not treated as
   hostile, and "text that argues for its own classification, can move the answer." A
   ticket opening "quick one-line fix" pushes toward the cheap tier. Today's Haiku prompt
   pushes back in prose ("Do not inflate reach or impact for small tickets"); with typed
   questions there is no prose channel to push back in, so the counter-pressure has to live
   in the `criteria` wording.
4. **No arithmetic identities.** Jaggedness §8: a question and its negation summed to 1.19,
   not 1.0, and Noul/Choice thresholds are not portable between primitives. Do not derive
   one factor from another, and do not reuse the 0.5 confidence floor across question types
   without checking it.
5. **`confidence` is overloaded in this codebase.** The RICE factor named `confidence`
   scores the ticket's clarity; Jev's `confidence` scores its own certainty. §3 renames the
   former to `clarity` for exactly this reason. Keep them apart in logs and events too.
6. **Debug logging is not body-redacted.** `sdk/python/usage.md` states that at `debug`
   level, secret *headers* are redacted but request and response bodies are **not**. Ticket
   text would land in logs. Do not enable SDK debug logging in the deployed engine.
7. **First non-LiteLLM AI call in the system.** Architectural, not incidental — call it out
   in review rather than letting it arrive as a surprise.
8. **`minions/` has mixed line endings** and no `.gitattributes`. `classifier.py` is LF
   (VERIFIED: `file minions/classifier.py`), so editing it is safe, but a whole-file
   rewrite of a CRLF sibling would show as a total-file diff.

## 8. Effort

| Piece | Size |
|---|---|
| `minions/classifiers/jev.py` — questions, call, recombination, cost | ~150 lines |
| Backend switch + shadow mode in `classifier.py` | ~40 lines |
| Config fields + `.env.example` + preflight check | ~30 lines |
| `scripts/classifier_corpus.sql` + replay harness | ~120 lines |
| Tests (fake `system_one`, threshold table, confidence gate, shadow isolation) | ~250 lines |

Roughly a day to shadow mode. The calibration in §5 is the long pole and is gated on
corpus size, which is still unknown.
