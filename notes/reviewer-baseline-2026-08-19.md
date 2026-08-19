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

**Model routing is a bigger lever than relocation, and it is nearly free.**
Reviewers run on *both* Opus 5 and Sonnet 5, at roughly a 50/50 split of runs
but a 72/28 split of cost: $0.666 vs $0.267 per run, 2.5x. Opus also takes 145s
against Sonnet's 88s. Routing reviewers to Sonnet is a config change with no new
infrastructure and would cut reviewer spend on the order of 60%.

What is NOT established: whether Opus reviews *better*. Nothing here splits
verdict quality by model, so the saving is only free if quality holds. That
measurement is the prerequisite, not a follow-up — and it is cheap, because
both models are already in the sample.

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

## Ordering

1. **Measure verdict quality by model.** Already possible from existing data.
2. **Route reviewers to the cheaper model if quality holds.** Config only, ~60%
   of reviewer spend, no infrastructure.
3. **Then** consider a herdr fan-out — bigger win if it moves spend to
   subscription capacity, but it is real engineering and its value is partly
   pre-empted by (2).
4. **Throughput last.** Raising the pull rate before the 33% success rate
   improves buys more failures, and failures already cost more than successes
   ($2.73 vs $1.46 mean on `easy` jobs).
