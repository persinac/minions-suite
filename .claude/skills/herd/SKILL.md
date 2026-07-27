---
name: herd
description: Claim one engineering work item from the minions queue, implement it, and report back over MCP. The subscription-billed alternative to an in-process LiteLLM engineer. Invoke when the user wants to run a minions ticket as the herder.
user-invocable: true
disable-model-invocation: true
argument-hint: "(no args — claims the next waiting item)"
---

# Herd

You are the **herder**: the engineer role for a minions job, running on a Claude
Code subscription instead of a metered API key.

Job `793821e8` spent **$10.66** on a ten-line security fix and merged nothing.
Orchestration — spec analyst and arbiter, the part minions does well — was
**$0.05** of that. The rest was inference. You are here to do that half for
effectively nothing, and to do it better.

## Preconditions

The engine must be publishing rather than running its own engineers:

```bash
kubectl exec -n minion-suite deploy/minion-suite -c minion-suite -- \
  /app/.venv/bin/python -c "from minions.config import Config; print(Config.from_env().engineer_dispatch)"
```

`external` means work is waiting to be claimed. `in_process` means the engine is
handling engineers itself and there will never be anything to claim — stop and
tell the user.

## 1. Claim

Call `claim_engineer_work` on the minions MCP server with a worker name
identifying this session.

`{"work": null}` means the queue is empty. Say so and stop — do not invent work.

The item contains everything needed: `task_id`, `job_id`, `agent_id`, `spec`,
`service`, `clone_url`, `default_branch`, `branch_name`, `pr_url`,
`is_revision`, and — on a revision — `review_feedback` already formatted as a
numbered findings checklist.

`engine_repo_path` is the **engine's** checkout inside its own container. Unless
you share that filesystem it does not exist for you — work from `clone_url`.

**You now own this task.** Nothing else will touch it until you report or
release, and the engine will run it in-process itself after
`herder_claim_timeout_seconds` if you go silent.

## 2. Work

Get a checkout from `clone_url`. If a local clone already exists, **use a git
worktree** rather than switching branches in it — that tree may be the user's,
with its own uncommitted state, and a checked-out branch left behind is a mess
someone else has to find.

If `branch_name` is set, use it; otherwise cut `feat/job-<job-id-prefix>/<slug>`
from `default_branch`.

**Branch first, commit as you go.** Not stylistic: an engineer that runs out of
budget with uncommitted work on no branch loses everything, which is how three
earlier runs died. Committed work on a branch can always be finished later.

**If `is_revision`, the checklist is a contract.** Work every numbered finding
in order. For each one, either fix it or state plainly why you are declining it.
Declining with a reason is fine. Silently skipping is what killed `793821e8` —
its revision agent fixed one finding of three, and the two it ignored came back
verbatim and unanimously for two more rounds until the job hit `max_revisions`.

**Restoring something a previous revision deleted counts as a fix.** If a
finding says a test was removed, put it back. Do not argue it was unnecessary —
that argument has already been lost three times.

**Never delete existing tests to make a change fit.** That exact regression got
`793821e8` rejected: it added 15 tests and quietly removed 3, and CI passed it.
Before you push, diff the test function names against the base branch:

```bash
git diff origin/<default_branch>...HEAD -- '*test*' | grep -E "^-\s*def test_"
```

Any output is a removed test. Restore it or justify it in the PR body.

## 3. Report

Push, open the PR, then **both** of:

1. `report_pr(task_id, pr_url, pr_number, branch_name)`
2. `complete_engineer_work(agent_id, summary)`

**Nothing downstream happens until `report_pr` lands.** The PR exists on GitHub
but the state machine cannot see it, so the task sits in `IN_PROGRESS` until the
claim times out and an in-process agent redoes your work.

**And the claim is not closed until `complete_engineer_work` lands.** Skipping it
deadlocks the revision loop: `claim_engineer_work` will not re-offer a task that
still has a live agent, and the engine's revision dispatcher will not dispatch
one either — each defers to the other and the job stops with reviewers already
voted. That happened on the first real run and had to be cleared by hand.

Reporting the PR without closing the claim is the single easiest way to wedge a
job. Do both.

## If you cannot finish

Call `release_engineer_work(agent_id, reason)`. Rate-limited, blocked, out of
depth — all fine, and all better than going quiet. Releasing frees the task
immediately; silence makes the engine wait out the full timeout first.

## Boundaries

- **Do not merge.** The minions merge gate owns that: it checks required status
  checks and reviewer verdicts, and it is the thing that stopped a bad PR
  landing. You are the author, not the gate.
- **Do not review your own work.** Reviewers run separately and independently
  on purpose. Your opinion of your own diff is worth less than theirs.
- **One item per invocation.** Claim, finish, report, stop.
