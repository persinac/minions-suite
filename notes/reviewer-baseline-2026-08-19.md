# Reviewer baseline — 2026-08-19

The "before" for any change to where or how reviewers run: model routing, a
herdr fan-out, a narrower fanout, a different revision cap. Captured *before*
anything moved, because once reviewers relocate this is unrecoverable.

Regenerate with `scripts/reviewer_baseline.py` — same queries, so before and
after are comparable. Do not hand-edit the numbers below; re-run and paste.

```
reviewer baseline @ 2026-08-19T06:17:05+00:00  schema=minions

== A. reviewer agents: cost / turns / cache / wall-clock, by model ==
  claude-opus-5                  n=  49 avg=$ 0.666 tot=$  32.64 (72.3%) turns= 9.2 cache=35.7%   145s
  claude-sonnet-5                n=  47 avg=$ 0.267 tot=$  12.53 (27.7%) turns= 7.8 cache=31.7%    88s
  TOTAL                                                   tot=$  45.17

== B. reviewer runs by host ==
  (unset)                                  n=  96  tot=$  45.17

== C. verdict distribution ==
  approve                52   51.5%
  request_changes        28   27.7%
  (empty)                21   20.8%
  TOTAL                 101

== D. by specialty (reviewer lens) ==
  api                      n=  31 avg_comments= 0.0 approve= 48.4%
  backend-architecture     n=  31 avg_comments= 0.0 approve= 58.1%
  pythonista               n=  29 avg_comments= 0.0 approve= 51.7%
  (none)                   n=   7 avg_comments= 0.0 approve= 28.6%
  dba                      n=   3 avg_comments= 0.0 approve= 66.7%

== E. revision rounds ==
  rounds=0  tasks= 161   76.7%
  rounds=1  tasks=  23   11.0%
  rounds=2  tasks=  14    6.7%
  rounds=3  tasks=  12    5.7%
  -> 49/210 (23.3%) of tasks needed at least one revision
```

## What the numbers say

**Reviewers are the largest cost centre.** Across `easy` development jobs they
are 59% of spend — more than engineers. That is what makes them the obvious
target, but see the ordering argument below before acting on it.

**Model routing was already fixed — do NOT read section A as a live lever.**
The all-time split (Opus 49 runs / $32.64, Sonnet 47 / $12.53) is a *historical*
population, and reading it as current state is the trap this paragraph exists to
stop. Broken out by date, every reviewer run since **2026-07-28** has been
Sonnet; the Opus runs are all 2026-07-25..27:

```
2026-08-17  claude-sonnet-5  30  $10.19
2026-08-14  claude-sonnet-5   3  $ 0.76
2026-08-13  claude-sonnet-5   3  $ 0.21
2026-08-12  claude-sonnet-5   2  $ 0.19
2026-07-28  claude-sonnet-5   6  $ 0.76
2026-07-27  claude-opus-5    30  $16.31   <- last Opus day
2026-07-26  claude-opus-5    17  $11.46
2026-07-25  claude-opus-5     2  $ 4.87
```

Config agrees: `model_reviewer` defaults to `claude-sonnet-5` (`config.py:226`),
the live value is `claude-sonnet-5`, `MODEL_REVIEWER` is unset, and there is
exactly ONE reviewer launch site (`dev.py:1177`) which correctly passes
`is_reviewer=True`. So the reviewer floor is real and already cheap.

**Consequence: the cheap config lever is spent.** Current reviewer economics are
~$0.267/run all-time on Sonnet, ~$0.34/run on the most recent day. Any further
reviewer saving needs a genuinely different substrate or vendor — which *raises*
the relative value of the herdr fan-out rather than pre-empting it.

Any future section-A comparison must be **date-bounded**. The all-time table
mixes two regimes and will keep suggesting a saving that was already taken.

**23.3% of tasks need at least one revision, and 12 tasks sat at exactly 3** —
the `max_revisions` cap. Those are jobs that burned the full budget and did not
land. Any throughput increase multiplies this population first, which is why
throughput should follow reliability rather than lead it.

## Two data-quality caveats — read before trusting the quality metrics

**`comments_posted` is 0.0 for every specialty.** Either the counter is not
written or reviewers genuinely post no inline comments. Production is GitHub,
where `post_inline_comment` degrades to a regular PR comment (no true inline
support), so this may be real behaviour rather than a telemetry gap — but it is
unverified either way. Do not use `comments_posted` as a quality signal until
someone establishes which it is.

**20.8% of reviewer tasks have an empty verdict.** A fifth of the gate reports
nothing. `tests/test_missing_verdicts.py` exists, so this is known, but it means
the approve/request_changes split is computed over ~79% of the population. Any
before/after comparison must hold this rate constant or it will read a change in
*reporting* as a change in *strictness*.

## Reviewer agreement — is the fan-out earning its cost?

Over PRs that received 2+ non-empty verdicts (n=17):

```
unanimous          11   64.7%
split               6   35.3%
unanimous APPROVE   9   52.9%   <- the fan-out bought nothing on these
fanout width: {2: 2, 3: 9, 5: 1, 6: 1, 8: 2, 11: 2}
```

A 35% split rate is the fan-out doing real work. But **53% unanimous-approve is
redundancy** — three reviewers agreeing to approve costs 3x and yields 1x.

The width distribution matters more than the base fan-out: the nominal width is
3, yet two PRs accumulated **eleven** reviewer verdicts. That is revision rounds
stacking reviewers, not the configured fan-out — so the reviewer cost tail is
driven by the revision loop, the same mechanism behind the 12 tasks that sat at
`max_revisions=3`.

**The value of N reviewers is their independence, not their number.** Three
same-family reviewers with different prompts are more correlated than three
different vendors with the same prompt. That makes vendor diversity a
*coverage* argument, not only a cost one — and it is falsifiable: if adding a
cross-vendor reviewer does not raise the split rate, the vendors are correlated
and the second bill bought nothing.

## Ordering

1. ~~Route reviewers to the cheaper model.~~ **Already done** (Sonnet since
   2026-07-28). See the correction above.
2. **Decide the fan-out width before the substrate.** 53% unanimous-approve
   suggests width is the cheaper question, and it is a config change.
3. **Then** the herdr fan-out / multi-vendor work. With (1) spent, this is now
   the main remaining reviewer lever rather than a partly-redundant one. Judge a
   vendor swap on the split rate and the empty-verdict rate, not on sticker price.
4. **Throughput last.** Raising the pull rate before the 33% success rate
   improves buys more failures, and failures already cost more than successes
   ($2.73 vs $1.46 mean on `easy` jobs).

## If the reviewer moves off Anthropic, watch these three

- **Cost attribution.** `cost_usd` comes from LiteLLM's cost map. A vendor it
  prices wrongly — or at zero — goes *invisible* in this baseline rather than
  wrong-but-visible. $0.00 rows already exist in the data (`database_engineer`),
  so there is no "zero means broken" alarm to rely on.
- **Prompt caching.** The ~32-36% cache read rate in section A is Anthropic-side.
  A vendor without equivalent caching can cost more per review at a lower sticker
  price. Compare effective cost per run, never list price.
- **Verdict format compliance.** The 20.8% empty-verdict rate is the canary. A
  vendor that cannot reliably emit the expected verdict shape does not weaken the
  gate — it *silently removes* it, and the job still reports success.
