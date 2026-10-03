"""The contract a scout finding must satisfy before it becomes a card.

A finding goes on the board as work someone -- usually a minion -- will pick up
and do. A vague one costs a whole job to discover it was vague. So the shape is
enforced at the tool boundary, not asked for in the prompt (memory:
prompt-text-is-not-a-contract): `submit_scout_finding` refuses a finding that
breaks it and returns the remedy, in the same spirit as `spec_contract.py`.

What it insists on, and why:

- **evidence** is `path:line` the scout actually read. A finding with no place
  in the code is an opinion.
- **oracle** says how to prove the work is done, as something someone can run
  whose result would CHANGE once it is fixed. "Tests pass" and "CI is green"
  are refused: they restate the process, not the outcome, and they pass on a
  change that did nothing.
- **fingerprint** is a stable name for the finding, so the same hot spot found
  next week is recognised as already filed rather than filed again.
"""

import re

# Where findings land, and how they are marked. NOT On-deck: putting a card in
# front of the line is the gate's decision (groomer today, weight later), and
# providers/trello_cards.py refuses the queue lanes and the `minion` label.
INBOX_LANE = "Inbox"
SCOUT_LABEL = "source:scout"

# How far back a fingerprint counts as already filed.
DEDUPE_DAYS = 90

# The kinds a scout may file. A fixed set, deliberately: an "other" kind is
# where vague findings go to hide.
MISSING_TEST_ORACLE = "missing_test_oracle"
KINDS: frozenset[str] = frozenset(
    {
        MISSING_TEST_ORACLE,  # repo has no test_command; each one unlocks a repo for the line
        "hot_spot",  # high churn x size file that keeps needing change
        "todo_cluster",  # a file whose TODO/FIXMEs name real unfinished work
        "dead_code",  # unreferenced code, with the search that shows it is unreferenced
        "error_handling",  # a swallowed or mis-handled error path
        "test_gap",  # code on a hot path with no test touching it
    }
)

MAX_TITLE_CHARS = 120
MIN_ORACLE_CHARS = 25

# `path:line` or `path:start-end`. The path has no spaces and no colon; the
# line is a number. Matches the form agents already see in tool output.
_EVIDENCE = re.compile(r"^[^\s:]+:\d+(-\d+)?$")

# Lowercase, starts with a letter or digit, then letters, digits and : . _ / -.
# Long enough to say something, short enough to index.
_FINGERPRINT = re.compile(r"^[a-z0-9][a-z0-9:._/-]{5,199}$")

# Oracles that describe the process instead of the outcome. Compared after
# lowercasing and stripping punctuation, so "Tests pass." is caught too.
_PROCESS_ORACLES = frozenset(
    {
        "tests pass",
        "all tests pass",
        "the tests pass",
        "ci is green",
        "ci passes",
        "ci green",
        "build passes",
        "it works",
        "see the diff",
        "lint passes",
        "code review approves",
    }
)


class ScoutFindingError(ValueError):
    """Raised when a finding breaks the contract. `remedy` is handed to the agent verbatim."""

    def __init__(self, remedy: str):
        self.remedy = remedy
        super().__init__(remedy)


def normalise_fingerprint(repo: str, fingerprint: str) -> str:
    """The stored form: scoped to the repo, so two repos can share a path."""
    return f"{repo.strip().lower()}:{fingerprint.strip().lower()}"


def validate_finding(kind: str, title: str, evidence: list[str], scope: str, oracle: str, fingerprint: str) -> None:
    """Raise ScoutFindingError naming the FIRST problem and how to fix it."""
    if kind not in KINDS:
        raise ScoutFindingError(f"Unknown kind {kind!r}. Use one of: {', '.join(sorted(KINDS))}.")

    clean_title = (title or "").strip()
    if not clean_title:
        raise ScoutFindingError("The finding has no title. Write one line saying what is wrong and where.")
    if len(clean_title) > MAX_TITLE_CHARS:
        raise ScoutFindingError(f"The title is {len(clean_title)} characters; keep it under {MAX_TITLE_CHARS}. Put detail in `scope`.")

    if not evidence:
        raise ScoutFindingError(
            "The finding has no evidence. Add at least one `path:line` you actually read, e.g. `src/app/routes.py:142`. "
            "A finding with no place in the code is an opinion."
        )
    bad = [e for e in evidence if not _EVIDENCE.match(str(e).strip())]
    if bad:
        raise ScoutFindingError(f"Evidence must be `path:line` or `path:start-end`. These are not: {bad[:3]}.")

    if not (scope or "").strip():
        raise ScoutFindingError("The finding has no scope. Say what one PR would change to fix it, and what it would leave alone.")

    clean_oracle = (oracle or "").strip()
    if not clean_oracle:
        raise ScoutFindingError(
            "The finding has no oracle. Say how someone proves the fix worked: a command or check whose "
            "result would come out DIFFERENT once this is fixed."
        )
    if re.sub(r"[^a-z ]", "", clean_oracle.lower()).strip() in _PROCESS_ORACLES:
        raise ScoutFindingError(
            f"The oracle {clean_oracle!r} describes the process, not the outcome — it would pass on a change that "
            "did nothing. Name a check whose result changes once this is fixed, e.g. a test that fails today."
        )
    if len(clean_oracle) < MIN_ORACLE_CHARS:
        raise ScoutFindingError(
            f"The oracle is too short to check ({len(clean_oracle)} characters). Name the exact command, test, or file "
            "someone would look at, and what they would see change."
        )

    if not _FINGERPRINT.match((fingerprint or "").strip().lower()):
        raise ScoutFindingError(
            "The fingerprint must be 6-200 characters: lowercase letters, digits and : . _ / -. Use "
            "`<kind>:<path>` or `<kind>:<path>:<symbol>`, e.g. `hot_spot:src/app/routes.py`."
        )
