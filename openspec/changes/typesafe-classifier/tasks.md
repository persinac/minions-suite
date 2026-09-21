# Tasks

Ordered so that every step before promotion is reversible and behaviour-neutral.
Nothing here changes which model a job gets until phase 4.

## Phase 0 — establish the oracle (no code, no key needed)

- [ ] Run `scripts/classifier_corpus.sql` against the deployed DB via the
      `kubectl exec` path in `docs/operator-setup.md`. The query is already verified
      against the real schema and against reason strings emitted by the real
      `classify_difficulty()` (see its header), so expect it to run as-is — what is
      unknown is how many rows come back.
- [ ] Record N (jobs with a parseable RICE reason **and** a non-empty spec). If N < 50,
      calibration is not fittable yet and phase 3 waits on shadow mode to accumulate rows.
- [ ] Sanity-check the extraction: the four factors should satisfy
      `effort / rice_confidence == ec_ratio` to rounding, and `tier` should agree with
      `tier_persisted`. A mismatch means the reason f-string drifted and the regexes
      need updating before anything is fitted to them.

## Phase 1 — plumbing

- [ ] Add `typesafe-sdk` to `pyproject.toml`; `task setup:uv-all`.
- [ ] `Config`: `typesafe_api_key`, `classifier_backend` (`litellm` | `typesafe` |
      `shadow`, default `litellm`), `classifier_min_confidence` (default 0.5).
      Follow the existing `_env_or*` pattern in `config.py:542-544`.
- [ ] `.env.example` + `docker-compose.yml` passthrough for `TYPESAFE_API_KEY`.
- [ ] Preflight check in `minions/preflight.py`: key present and `GET /v1/models`
      reachable. **Warn-only** — the classifier is fail-open and must never gate startup.

## Phase 2 — the Jev backend

- [ ] `minions/classifiers/jev.py`:
  - [ ] The four `Score` questions from `design.md` §3, as module constants.
  - [ ] `AsyncTypeSafeClient` call — one `system_one(state, questions)` for all four.
  - [ ] `levels_to_difficulty()`: normalize by `len(criteria) - 1`, then the threshold
        rules from §4. Pure function, no I/O — mirror how `score_to_difficulty()` is
        testable in isolation.
  - [ ] Confidence gate: any of the four below `classifier_min_confidence` → return
        `None`, which `resolve_model()` already routes to the medium tier.
  - [ ] `_jev_cost(usage)` — $0.042/Mtok on **input only**. Do not call
        `litellm.completion_cost()`; it returns `0.0` for a non-LiteLLM provider.
  - [ ] Catch `TypeSafeAPIError` and everything else; log and return `None`. Same
        fail-open contract as the LiteLLM path.
- [ ] Backend switch inside `classify_difficulty()`. Keep the `(difficulty, reason)`
      return contract byte-compatible — `minions/engine/dev.py:643` must not change.
- [ ] Reason string for the Jev path: include all four normalized scores **and** their
      confidences, so a shadow row is self-describing without a join.

## Phase 3 — shadow mode and calibration

- [ ] `shadow`: run both, return the LiteLLM verdict, record Jev's as a
      `difficulty_shadow` event. A Jev exception must be swallowed — assert this with a
      test that raises from the fake and asserts the LiteLLM verdict still returns.
- [ ] Deploy at `CLASSIFIER_BACKEND=shadow`. Measure real latency; the docs publish no
      figure ("adding questions barely changes the response time" is the only claim).
- [ ] Replay harness over the phase-0 corpus. Emit: confusion matrix of Jev tier vs
      outcome, disagreement rate vs Haiku, and the disagreements sorted by realized cost.
- [ ] Fit the six thresholds in §4 against **outcomes**, not against Haiku's verdicts.
- [ ] Sweep `CLASSIFIER_MAX_CHARS` downward. Jaggedness §5 says accuracy falls as the
      state grows with irrelevant content, so 6000 may be worse than 2000 here — the
      opposite of the intuition for a chat model.
- [ ] Decide. The kill signal is a disagreement rate that is high *and* not explained by
      outcomes, or an `effort` score that fails to separate ceiling-hitting jobs from
      cheap ones. Either means keep Haiku, and that is an acceptable result.

## Phase 4 — promotion (gated on phase 3)

- [ ] Flip one project to `CLASSIFIER_BACKEND=typesafe` via `projects.yaml`, not globally.
- [ ] Watch `difficulty` distribution and cost-per-success for a week against the shadow
      baseline.
- [ ] Global default only after that. Keep the `litellm` backend in place permanently —
      it is the fallback if TypeSafe has an outage or changes its pricing.

## Tests

- [ ] `levels_to_difficulty()` threshold table: every tier boundary, both sides.
- [ ] Stakes guard in ordinal form: high blast radius + high consequence lifts easy to
      medium; either one alone does not.
- [ ] Confidence gate returns `None`, and `resolve_model(None, ...)` still lands on
      `model_medium` — the existing fail-open path, re-asserted here because this change
      adds a new way to reach it.
- [ ] Shadow isolation: a **raising** fake Jev client leaves the returned verdict equal to
      the LiteLLM one. (Use a counting spy, not just a raising mock — `gather(...,
      return_exceptions=True)` swallows exceptions elsewhere in this codebase and a
      raising fake can pass vacuously.)
- [ ] Cost: a known `usage` payload produces the documented $0.042/Mtok figure, and
      asserts output tokens contribute **zero**.
- [ ] Backend switch: `litellm` never constructs a TypeSafe client. Assert by spy, so the
      test fails if the client is built eagerly at import.

## Out of scope

Adjacent fits worth noting and **not** doing here: reviewer verdict aggregation
(`aggregate_verdicts`), the `## Assumptions` contract check in
`minions/core/spec_contract.py`, and `report_no_work_needed` triage. All three are
classification problems currently solved with prose parsing. Each should be judged on its
own evidence after the classifier either earns its place or does not.
