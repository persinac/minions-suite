## Why

`minions/classifier.py` routes every dev job to a model tier from one Haiku call. The
call asks for four RICE factors as a JSON object, and `_parse()` then defends against
code fences, non-objects, missing keys, and non-numeric values — because a chat model
returns prose that *might* be JSON. Every one of those failure paths ends in
`difficulty = None`, which silently drops the job to the medium tier.

TypeSafe's Jev (`jev-1.13.0`) is a classifier-shaped model: you send a `state` plus typed
questions and get back typed values, probability distributions, and a per-answer
`confidence`. There is no text to parse, so the entire `_parse()` failure class stops
existing. It also hands us something we cannot get today: a signal for *how certain the
scoring itself was*, which is a different question from the RICE `confidence` factor
(that one scores how clearly the **ticket** specifies its approach).

The economics are real but secondary — documented at $0.042/Mtok input with output free,
against ~$1/Mtok input for Haiku 4.5, so roughly a 20x reduction on a call that is already
fractions of a cent. The reasons to do this are reliability and the new confidence signal,
not the money.

The adoption risk is unusually well-bounded here. `classify_difficulty()` is fail-open by
construction — its docstring says it "can only make a job cheaper or leave it unchanged" —
so a TypeSafe outage degrades to the medium tier rather than failing a job. That is a good
risk profile for a young vendor with one model and no self-hosting option.

## What Changes

**This is not a 1:1 port, and that is the central finding.** TypeSafe's own jaggedness
page for `jev-1.13` states that "jev-1.13's score levels are weak in numerical
calibration" and that "interpolating between adjacent levels to recover an exact number
won't work." Our `D = effort / confidence` does exactly that twice, then divides by the
weaker of the two magnitudes. Porting the formula unchanged is contraindicated by the
vendor's documentation.

So the arithmetic changes shape while the decomposition stays:

- Add a `CLASSIFIER_BACKEND` switch — `litellm` (default, unchanged), `typesafe`, `shadow`
- Add `minions/classifiers/jev.py`: four typed questions in one `system_one` request
- Replace magnitude arithmetic (`effort / confidence`) with **threshold checks on
  normalized expectations**, which the same page explicitly sanctions
- Gate on the new per-answer `confidence`: below 0.5, return `None` and take the existing
  fail-open path to the medium tier
- Compute cost locally from the documented rate — `litellm.completion_cost()` cannot
  price a non-LiteLLM provider and returns `0.0`
- `shadow` mode runs both backends, returns the LiteLLM verdict, and records Jev's as a
  `difficulty_shadow` event — zero behaviour change, builds a comparison corpus

`minions/engine/dev.py` does not change. `classify_difficulty()` stays the single entry
point with the same `(difficulty, reason)` contract.

## Capabilities

### New Capabilities
- `typesafe-classification`: Jev-backed difficulty scoring — typed questions, ordinal
  recombination, confidence gating, local cost accounting, and the shadow-mode comparison

### Modified Capabilities
- `classifier.py` gains a backend switch; `score_to_difficulty()` and the LiteLLM path are
  untouched

## Impact

- **Dependencies**: `typesafe-sdk` (Python, async client available)
- **Config**: `TYPESAFE_API_KEY`, `CLASSIFIER_BACKEND`, `CLASSIFIER_MIN_CONFIDENCE`.
  Default `litellm` = no behaviour change without deliberate opt-in
- **Architecture**: this is the first AI call in the system that does **not** go through
  LiteLLM. That is a deliberate breach of the vendor-agnostic abstraction and should be
  named as such in review — Jev is not a chat-completion provider and cannot be adapted
  to one
- **Cost ceilings are NOT affected.** `assert_priceable()` is called only at
  `minions/agents/runner.py:225` for *agent* models. The classifier's cost
  (`classifier.py:199`) is logged for observability and never feeds
  `agent_cost_limit_usd` or `job_cost_limit_usd`, so an unpriceable classifier cannot
  make a ceiling inert
- **Recalibration required**: `EASY_MAX = 3.0` / `MEDIUM_MAX = 8.0` are calibrated against
  the E/C ratio and have no meaning in level space. The replacement thresholds are
  **uncalibrated guesses until validated against the replay corpus** (see `design.md`)
- **No breaking changes** at the default setting
