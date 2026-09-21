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

- [x] Add `typesafe-sdk` to `pyproject.toml` (0.7.0; `requires-python >=3.10`).
      Note `typesafe` on PyPI is an unrelated package and `typesafe-ai` is a redirect
      shim — the vendor's own install line is `uv add typesafe-sdk`.
- [x] `Config`: `typesafe_api_key`, `typesafe_model`, `typesafe_timeout`,
      `classifier_backend`, `classifier_min_confidence`, via the existing `_env_or*`
      pattern. Defaults keep `litellm`, so behaviour is unchanged until opted in.
- [x] `.env.example` gains `TYPESAFE_API_KEY`; the three compose services already
      use `env_file: .env`, so no per-var passthrough was needed. Tuning knobs went
      to `settings.toml [engine]`, matching where `classifier_model` lives.
- [ ] Preflight check in `minions/preflight.py`: key present and `GET /v1/models`
      reachable. **Warn-only** — the classifier is fail-open and must never gate startup.

## Phase 2 — the Jev backend

Landed as `minions/classifier_jev.py`, not `minions/classifiers/jev.py` — a
`classifiers/` package sitting beside the existing `classifier.py` module reads as a
typo at every import site.

- [x] The four `Score` questions as module constants, built lazily in
      `build_questions()` so importing the module never requires typesafe-sdk.
- [x] `AsyncTypeSafeClient` call — one `system_one(state, questions)` for all four.
- [x] `levels_to_difficulty()`: normalizes by `len(criteria) - 1` internally, then the
      threshold rules from §4. Pure, no I/O.
- [x] Confidence gate on the weakest of the four answers, naming which question was
      weakest so a gated row says *why*.
- [x] `jev_cost_usd(usage)` — input only. `Usage.input_tokens` is `int | None` in the
      SDK, so a missing count reads as 0.0 rather than raising inside a fail-open path.
- [x] Fail-open on `ImportError`, a missing key, any exception, and a response missing
      an expected answer.
- [x] Backend switch inside `classify_difficulty()`, defaulting to `litellm`. An
      unrecognised value also falls back to `litellm` rather than erroring.
- [x] Reason string carries all four raw scores over their level maxima, the two
      normalized drivers, and the weakest confidence.

Deviation from the proposal: `dev.py` **did** change, by one line. Shadow mode has to
write an event, and `classify_difficulty` had no database handle. It now takes optional
`db` / `job_id`; omitting them loses the comparison, not the verdict.

## Phase 3 — shadow mode and calibration

- [x] `shadow`: runs both, returns the LiteLLM verdict, records Jev's as a
      `difficulty_shadow` event carrying scores, confidences, probabilities, cost, and
      an `agreed` flag. A Jev exception, and a failing event write, are both swallowed.
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

33 tests in `tests/test_classifier_jev.py`. Fakes are built from the SDK's own
`ScoreAnswer` / `SystemOneResponse` / `Usage` models, so a shape change in
typesafe-sdk breaks them rather than passing them.

- [x] `levels_to_difficulty()` threshold table, plus the `HARD_EFFORT` boundary from
      both sides. Note 2.01/3 is 0.6699999… in binary float, so the "just over" case
      uses 2.1 — the first attempt asserted 2.01 and failed for that reason.
- [x] Stakes guard in ordinal form: both factors high lifts easy to medium; either
      alone does not.
- [x] Confidence gate returns `None`, and `resolve_model` then lands on `model_medium`
      and explicitly not on `config.model`.
- [x] Shadow isolation with a **counting** spy: asserts `client.calls == 1` alongside
      the surviving verdict, because `gather(return_exceptions=True)` swallows the raise
      and a raising fake that is never reached would otherwise pass vacuously.
- [x] Cost: 1M input tokens equals the documented rate; output tokens change nothing;
      `None` usage and `None` input_tokens are 0.0.
- [x] Backend switch: the `litellm` path raises from a spy if it ever constructs a
      TypeSafe client, so an eager module-scope client would fail the test.
- [x] Negative control (in memory, nothing written to disk): mutating `HARD_EFFORT`,
      `EASY_CLARITY`, `STAKES_BLAST` and `INPUT_USD_PER_MTOK` each changes an outcome,
      so none of those thresholds is decorative.

One real bug surfaced here rather than in review: a Jev *API error* returns empty
telemetry, so the shadow record had no `difficulty` key at all — a hole in the corpus
rather than a recorded "no verdict". `_classify_shadow` now defaults it.

## Out of scope

Adjacent fits worth noting and **not** doing here: reviewer verdict aggregation
(`aggregate_verdicts`), the `## Assumptions` contract check in
`minions/core/spec_contract.py`, and `report_no_work_needed` triage. All three are
classification problems currently solved with prose parsing. Each should be judged on its
own evidence after the classifier either earns its place or does not.
