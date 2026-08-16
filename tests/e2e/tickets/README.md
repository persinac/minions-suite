# Ambiguous ticket corpus

Deliberately under-specified tickets, for driving live e2e runs against a scratch
repo. Each one is written the way a real ticket gets written — by someone who knew
more than they typed.

They are grouped by the *kind* of gap they contain, because the kinds fail
differently and a corpus that is all one kind will look like broad coverage while
testing one behaviour:

| File | Kind | The gap |
|---|---|---|
| `missing-bound.md` | Missing bound | A quantity with no number — "recent", "large", "slow" |
| `undefined-semantics.md` | Undefined semantics | A verb with more than one meaning — "delete", "archive", "reset" |
| `unstated-scope.md` | Unstated scope | Where the change applies is never said |
| `conflicting-constraint.md` | Conflicting constraint | Two requirements that cannot both hold |
| `false-precision.md` | False precision | Specific-sounding numbers that do not determine the design |

## What a good run looks like

Not "the agent picked the option I would have picked" — several readings are
usually defensible, and grading on agreement rewards luck. Grade on whether the
guess was *declared and grounded*:

1. The refined spec has an `## Assumptions` section (enforced by
   `minions/core/spec_contract.py`, so its absence is a bug, not a judgment call).
2. Each assumption names the ambiguous phrase, the reading chosen, and a reason
   traceable to something real — a repo convention, an analogous feature, or
   reversibility.
3. The reasons are checkable against the scratch repo. "Matches how `users`
   soft-deletes" is verifiable; "seems better" is not.
4. The job reaches a terminal state without stalling.

`conflicting-constraint.md` is the one that should NOT produce a confident spec.
Both requirements cannot hold, so the correct behaviour is to say so — an analyst
that invents a tidy resolution and proceeds has failed that ticket even if the
resulting PR is clean.

## Running one

    task e2e:live -- --ticket missing-bound

See `tasks/e2e.yaml`. Live runs cost tokens and open real PRs; point them at a
throwaway repo, never at anything you would mind rewriting.
