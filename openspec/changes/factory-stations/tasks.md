# Tasks — factory stations

Phases are in build order. Each phase ships and runs live before the next one starts.

## 0. Pull the cord (separate PRs, in flight 2026-10-03)

- [ ] Abandoned-herder guard reaches the `changes_requested` branch (job `a9f2b36e`)
- [ ] Andon: `stalled_job`, `stale_claim`, `line_idle_with_queue` → Slack DM

## 1. Station rails

- [ ] `[engine.role_models]` table, declared in both `[default.engine]` and
      `[production.engine]`; verified by running the loader, not by grepping
- [ ] Station budget check before launch, plus a `station_budget_exhausted` event
- [ ] `scout` and `verify` job types with transition-map entries; invariant tests pass
- [ ] `/metrics`: `minion_station_runs_total{station,outcome}`, `minion_station_spend_usd{station}`

## 2. Scout

- [ ] `scout_signals`: churn × size, `lint_command`/`type_command`, TODO density,
      missing `test_command`, red non-required checks
- [ ] `submit_scout_finding` tool: refuses no-oracle findings and 90-day duplicate fingerprints
- [ ] `prompts/agents/scout.md`
- [ ] Trello `Inbox` lane + `source:scout` label; `scout_autoqueue = false`
- [ ] First live run on one repo; Alex reviews the findings
- [ ] Hardcoded first kind: `missing_test_oracle` across the allowlist

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
- [ ] `DRIFTED` → `Inbox` card; `UNFALSIFIABLE` → counted against the weight record
- [ ] `prompts/agents/verifier.md`
