# Tasks — factory stations

Phases are in build order. Each phase ships and runs live before the next one starts.

## 0. Pull the cord (separate PRs, in flight 2026-10-03)

- [x] Abandoned-herder guard reaches the `changes_requested` branch (job `a9f2b36e`) — #107
- [x] Andon: `stalled_job`, `stale_claim`, `line_idle_with_queue` → Slack DM — #109

## 1. Station rails

- [x] `[engine.role_models]` table, declared in both `[default.engine]` and
      `[production.engine]`; verified by running the loader, not by grepping
- [x] Station budget check before launch, plus a `station_budget_exhausted` event
- [x] `scout` job type (`scouting` status) with transition-map entries; invariant tests pass.
      `verify` is deferred to phase 5: a status with no producer is dead vocabulary
- [x] `/metrics`: `minion_station_runs_total{station,outcome}`, `minion_station_spend_usd{station}`
- [x] Stations stay off the line: excluded from intake capacity, cost-per-success, the
      stuck-task rule, and the andon slot-holder message

## 2. Scout

- [x] `scout_signals`: churn × size, TODO density, missing `test_command` (`engine/scout_signals.py`)
- [ ] `scout_signals`: `lint_command`/`type_command` output — deferred: needs each repo's
      dependencies installed in the engine pod (a sandbox question); recorded in the
      signals as `deferred`, never as a clean result
- [ ] `scout_signals`: red non-required checks — deferred: GitHub checks API per repo
- [x] `submit_scout_finding` tool: refuses no-oracle findings and 90-day duplicate fingerprints;
      also no-evidence, process-only oracles, the per-run cap, and a wrong repo
- [x] `prompts/agents/scout.md`
- [x] Trello `Inbox` lane + `source:scout` label (resolved or created by name); `scout_enabled` kill switch.
      `scout_autoqueue` is NOT a setting yet: autoqueue means weight-eligible scout cards go to
      `On-deck`, and there is no weight. It arrives with phase 3
- [x] Until phase 3 ships, the groomer's existing rubric is the gate. Scout cards land in
      `Inbox` and the groomer queues them on its normal pass; scout never writes to
      `On-deck` itself — `providers/trello_cards.py` refuses the queue lanes and the `minion` label
- [ ] First live run on one repo; check the `outcome` metric after a week
      (`minion_scout_findings_total{kind,outcome}`) — needs migration
      `20261003120000_add_scout_tables.sql` applied and a release
- [x] Hardcoded first kind: `missing_test_oracle` — the tool refuses any other kind for a repo
      with no `test_command` until that finding is filed

## 3. Weight

- [ ] `submit_weight` typed record + `weight_records` table
- [ ] Queue predicate in code; the groomer prompt reads it instead of deciding in prose
- [ ] Shadow Jev on weight, as on difficulty
- [ ] Weight-miss metric: `eligible` cards that ended `failed` or `no_work_needed`

## 4. Deploy watcher

- [ ] `deploy:` block schema in `projects.yaml`; fill in kinds from `.circleci/` and ArgoCD apps
- [ ] Deterministic pollers: argocd (synced to SHA + Healthy), circleci (workflow for SHA),
      ecs (task def image tag)
- [ ] `DEPLOYING → DEPLOYED | DEPLOY_FAILED`, with `deploy_timeout_seconds`
- [ ] Model-on-failure `deploy_failure` record; andon on every failure

## 5. Verifier

- [ ] `telemetry:` block schema; filled in for cns services, management-api,
      management-dashboard, minions-suite
- [ ] Read-only executors: Prometheus query, SigNoz query (traces/logs), `VERIFY:` runner
- [ ] `submit_verification` tool: refuses a verdict whose `queries[]` never touch the
      changed service
- [ ] `DRIFTED` → new `On-deck` card (minion label) linking the original card, job, and PR; `drift_depth` guard at `drift_max_depth` = 2 → andon
- [ ] `UNFALSIFIABLE` → counted against the weight record
- [ ] `prompts/agents/verifier.md`
