# Design — factory stations

## The shape

```
            ┌──────── supply ────────┐   ┌──────────── line (exists) ────────────┐   ┌──── after merge ────┐
 repos ──▶  SCOUT ──▶ Inbox ──▶ (groomer: triage) ──▶ WEIGHT ──▶ On-deck ──▶ analyst ▶ arbiter ▶ engineer ▶ review ▶ merge ──▶ DEPLOY ──▶ VERIFY
 humans ─────────────────────────▶ Inbox ───────────────┘                                                                    │           │
                                                                                                                             ▼           ▼
                                                                                                                       andon on fail   DRIFTED ▶ new card
                                                                                                                                       UNFALSIFIABLE ▶ weight learns
```

Human-filed cards and scout cards take the same path from `Inbox` on. Scout gets no
private lane into the queue.

## The station contract

Every station is defined by the same seven fields. A station missing one is not done.

| field | meaning |
|---|---|
| trigger | what starts a run (schedule, state transition, or card arrival) |
| profile | `prompts/agents/<role>.md`, written 8th-grade, in bullets |
| model | pinned in `[engine.role_models]`; never follows ticket difficulty |
| tools | the smallest set that does the job; read-only unless stated |
| output | a typed record written through **one** MCP tool that validates it |
| budget | `daily_usd` and `max_runs_per_day`; when exhausted, stop and record `station_budget_exhausted` |
| oracle | how we know the station itself is working (a metric on `/metrics`) |

The output rule carries over from the assumptions contract (`core/spec_contract.py`):
the tool refuses a malformed record and says what to fix. A prompt instruction is not a
contract; the tool boundary is (memory `prompt-text-is-not-a-contract`).

## Stations

### Scout

- **trigger:** engine schedule. Round-robin over allowlisted repos, oldest-scouted first,
  `scout_max_runs_per_day` (default 3).
- **two phases, on purpose:**
  1. *Signals* — deterministic, no model, no cost: `git log` churn × file size over 90 days;
     `lint_command` and `type_command` output if the repo declares them; TODO/FIXME density;
     a missing `test_command`; red non-required checks on the last 20 `main` runs;
     dependency lag where a lockfile makes it cheap. Stored as a `scout_signals` row.
  2. *Findings* — a model reads the code at the top-N signals and writes at most
     `scout_max_findings` (default 3) findings.
- **output tool:** `submit_scout_finding(repo, kind, title, evidence[file:line], scope, oracle, fingerprint)`.
  It refuses a finding with no oracle, and it refuses a fingerprint already filed in the
  last 90 days.
- **model:** sonnet. The work is reading code; haiku's record on that is the
  backend_engineer table.
- **first finding kind, hardcoded:** `missing_test_oracle`. Each one unlocks a whole repo
  for the line (tests-first rule), so it is worth more than any single lint fix.
- **oracle:** `minion_scout_findings_total{kind,outcome}`. `outcome` is what became of the
  card: queued, rejected by weight, closed by a human, or merged. A scout whose findings
  humans keep closing is visible in a week.

### Weight

- **trigger:** a card arrives in `Inbox` or `On-deck` with no weight record, or its
  description changed since the last one.
- **output tool:** `submit_weight(card_id, repo, has_oracle, needs_hardware, needs_human_decision,
  blast_radius{low,med,high}, loe{S,M,L}, confidence, verdict{eligible,ineligible}, reasons[])`.
- **the queue decision becomes code:** `eligible ∧ has_oracle ∧ ¬needs_hardware ∧
  ¬needs_human_decision ∧ repo ∈ allowlist ∧ blast_radius ≠ high`. The groomer stops
  deciding this in prose and just reads it. This is how "why wasn't this queued?" gets an
  answer.
- **model:** the classifier slot. Weight is typed questions and typed answers, which is
  what Jev was adopted for (`openspec/changes/typesafe-classifier/`). Ship on litellm/haiku
  and shadow Jev, as the difficulty classifier did.
- **oracle:** weight verdict against job outcome. An `eligible` card that failed or came
  back `no_work_needed` is a weight miss.

### Deploy watcher

- **trigger:** job reaches `MERGED` and the service's `deploy.kind ≠ none`.
- **deterministic poll, no model:** `argocd` means the app is synced to the merge commit and
  `Healthy`; `circleci` means the workflow for the merge SHA succeeded; `ecs` means the
  service's running task definition carries the new image tag. Timeout is
  `deploy_timeout_seconds` (default 1800).
- **model only on failure:** the `DEPLOY_MONITOR` profile reads the failing pipeline or the
  degraded app and writes a `deploy_failure` record. Andon fires either way.
- **why it failed before, and why it won't now:** the pre-0.8.53 monitor waited for a
  `deploy_target` that nothing produced, so it could never conclude. This one has a
  per-kind success predicate and a timeout. Every path ends in `DEPLOYED` or
  `DEPLOY_FAILED`.
- **`deploy:` blocks are data work:** derive each repo's kind from its `.circleci/` and the
  ArgoCD app list (the cluster carries `*-prod` apps). Repos we can't classify stay `none`
  and skip the station.

### Verifier

- **trigger:** `DEPLOYED`, or `MERGED` when `deploy.kind = none`; in that case only the
  scenario half runs.
- **inputs:** the refined spec (acceptance criteria and `## Assumptions`), the PR body's
  `VERIFY:` line, the deploy timestamp, and the service's `telemetry:` block.
- **two halves:**
  1. *Scenarios* — run the `VERIFY:` measurement, plus acceptance scenarios where a repo
     declares a `scenario_command`.
  2. *Honeycomb-style* — query SigNoz (`signoz-release:8080`, traces and logs) and
     Prometheus (`prometheus-server.observability`) for the signal the change claims to
     move, in a window before versus after the deploy. Use high-cardinality slicing
     (service, route, error type), not just a dashboard average.
- **output tool:** `submit_verification(job_id, verdict{CONFIRMED,DRIFTED,UNFALSIFIABLE}, evidence[], queries[])`.
  `queries[]` is required so a human can re-run what the verifier saw.
- **consequences:** `DRIFTED` files an `Inbox` card linked to the job. `UNFALSIFIABLE` is
  written back to the job and counted against the weight record that let it through.
- **model:** sonnet.
- **scope at first:** services with a `telemetry:` block — flashback-cns services,
  management-api, management-dashboard, and minions-suite itself. Firmware is out; it has
  no deployed telemetry path the verifier can read.

## Decisions

1. **Stations are minions roles, not more cron skills.** Same rails as the line: agent rows,
   cost, `get_model_effectiveness`, events, andon. The groomer stays a skill because it
   already works and is subscription-billed; only its queue decision moves into code.
2. **Deterministic first, model second, in every station.** Scout signals, the weight queue
   predicate, and the deploy poll are all code. A model runs only where judgment is needed.
   That keeps the budget small and the failure modes legible.
3. **One validated output tool per station.** No station's result is "whatever the agent said
   last" (memory `empty-verdicts-came-from-the-loop-exit`).
4. **Pinned models per role.** Difficulty routing exists because engineer cost scales with
   ticket difficulty. Station cost does not, so routing stations by difficulty would only add
   variance.
5. **External dispatch is optional, not first.** Herder claiming works for engineers and is
   live-gated for reviewers. Stations start in-process under hard budgets; external
   claiming for scout and verify is a later switch, using the same `peek`/`claim` tools.

## Risks

- **Scout floods the board.** Mitigated by `max_findings` per run, runs per day, 90-day
  fingerprint dedupe, weight gating, and the `outcome` metric. A human approves the first
  ~20 scout cards before scout output can reach `On-deck` (`scout_autoqueue = false` at
  launch).
- **The verifier claims CONFIRMED on noise.** A before/after window on a quiet service can
  show nothing either way. The tool requires `queries[]`. A verdict with no queries that
  touch the changed service is refused and comes back as `UNFALSIFIABLE`.
- **The deploy watcher wedges a job.** That is the 0.8.53 failure. Every wait has a timeout,
  and `DEPLOY_FAILED` is terminal. The andon `stalled_job` condition backstops it.
- **Station spend creeps into the line's budget.** Budgets are per station and checked
  before launch, not after.

## Open questions for Alex

1. Scout autoqueue: should a weight-`eligible` scout card ever reach `On-deck` without a
   human, and after how many approved ones? Proposal: off at launch, revisit after 20.
2. Verify `DRIFTED`: is a new `Inbox` card the right consequence, or should it reopen the
   original card?
3. Budget: is ≤ $3/day across all stations the right ceiling to start?
