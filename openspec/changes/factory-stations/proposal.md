## Why

Minions has a working **line** and almost nothing on either side of it.

The line — intake → spec analyst → arbiter → engineer → reviewer panel → merge — works.
Over the 14 days to 2026-10-03 it finished 30 of 34 jobs with zero real failures at
$0.69 per success (`get_model_effectiveness(days=14)`, measured 2026-10-03 17:47 UTC).

What is missing is everything a factory has that a line does not:

1. **Nothing makes work.** The board is human-filed and mostly firmware and hardware. The
   hourly groomer runs 24 times a day and queues almost nothing because nothing on the board
   qualifies (see memory `supply-not-capability-is-the-constraint`, 2026-09-21). The engine
   sat idle for 12 days in September for want of cards, not capability.
2. **Nothing checks that shipped work worked.** A job is `DONE` when its PR merges. The
   fleet convention puts a `VERIFY:` line in every PR body, and nothing ever runs it.
3. **Nothing watches the deploy.** `DEPLOY_MONITOR` was removed in 0.8.53 because it could
   never conclude. Since then `advance_merged_job` (`engine/deploy.py:52`) jumps
   `MERGED → DEPLOYED` and hands off to each repo's CD, and no one looks at the result.
   All 33 `deploy_target` entries in `projects.yaml` are `none`.
4. **Nothing pulls the cord.** From 2026-09-26 to 2026-10-03 the line was stopped by one
   stale herder claim (job `a9f2b36e`). The only thing that noticed was the groomer, writing
   "needs a human" to journald every hour. (Fixed separately: the claim fix and the andon
   alarm ship as their own PRs, not in this change.)

The target shape is Alex's work factory, which already runs these stations:
**scout → triage → weight → line → deploy → verify**, each with its own profile and model
so cost stays under control.

## What Changes

Add four **stations** around the existing line. Each is an agent role with a fixed contract:
a trigger, a profile prompt, a pinned model, a tool set, a typed output, and a daily budget.

- **Scout** (new `SCOUT` role) — on a schedule, picks a repo, collects cheap deterministic
  signals (churn hot spots, lint and type output, TODO density, dependency lag, missing test
  oracle, red non-required checks), then has a model read the code at those spots and write
  **findings**. Each finding carries evidence (`file:line`), a scope, and an oracle: how to
  prove it is done. Findings become Trello cards in an `Inbox` lane with a `source:scout`
  label, deduplicated by fingerprint.
- **Weight** (new `WEIGHT` role) — grades a card for whether *this factory* can carry it,
  as a typed record rather than prose: oracle present, needs hardware, needs a human decision,
  blast radius, LOE, confidence, verdict plus reasons. The queue decision becomes a
  deterministic read of that record. Every scout card goes through it. The groomer keeps
  triage (labels, dupes, already-fixed) and stops making the queue call in prose.
- **Deploy watcher** (`DEPLOY_MONITOR`, revived as an *observer*, not an actor) —
  `projects.yaml` gains a `deploy:` block (`argocd` / `circleci` / `ecs` / `none`). After
  merge, the engine polls deterministically until the merged version is live and healthy, or
  times out. A model runs only on failure, to read the pipeline and say why. `DEPLOYING` is
  real again, with a timeout and an andon alarm.
- **Verifier** (new `VERIFIER` role) — runs after the deploy is confirmed. It executes the
  PR's `VERIFY:` line and the spec's acceptance scenarios, and does honeycomb-style checks:
  it queries SigNoz traces and Prometheus for the signal the change should move,
  before versus after the deploy. Verdict: `CONFIRMED` / `DRIFTED` / `UNFALSIFIABLE`.
  `DRIFTED` files a card; `UNFALSIFIABLE` is recorded against the ticket, so weight learns
  which tickets ship without an oracle.
- **Per-role model pinning** — a `[engine.role_models]` table. Stations get a fixed model;
  engineers keep difficulty routing, because that is where routing pays.
- **Station budgets** — each station has a daily USD cap and a max-runs-per-day. A station
  that hits its cap stops and says so; it never borrows from the line.

## Impact

- New roles in `core/models.py`, new tool sets in `agents/tools/definitions.py`, and new
  profiles in `prompts/agents/`.
- New job types: `scout` and `verify` jobs, beside `dev` and `review`. Their state machines
  are added to the transition maps; the invariant tests in `test_transition_invariants.py`
  check them for free.
- `projects.yaml` schema: `deploy:` block, optional `lint_command`, `type_command`, and
  `telemetry:` (SigNoz service name, Prometheus selectors).
- New read-only tool executors for Prometheus, SigNoz, ArgoCD, and the CircleCI API.
- Trello: a new `Inbox` lane and `source:*` labels. The groomer prompt changes so the queue
  decision reads weight records.
- Cost: bounded by station budgets. Initial proposal is ≤ $3/day across all stations, beside
  the line's ~$1.50/day.

## Not in this change

- Raising `max_concurrent_jobs` (deliberately not picked).
- Moving the groomer itself into the engine. It stays a headless subscription-billed skill;
  only its queue decision changes.
- Auto-rollback. The deploy watcher reports and alarms; it never reverts.
