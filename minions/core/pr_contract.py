"""The contract a PR body must satisfy before the PR is accepted.

`report_pr` is the moment a task's work becomes a fact the rest of the pipeline
acts on. From there the reviewer fan-out, the merge gate and auto-merge all
treat the PR as real, and the PR body is the artifact a human reads months
later to find out what the change was supposed to prove.

`prompts/agents/engineer.md` has told engineers to end that body with a
`VERIFY:` line since it was written. Nothing checked. A convention that lives
only in a prompt file looks adopted -- it is in the repo, it is in the prompt,
an agent can be seen following it -- while gating nothing, which is the same
shape as `service.test_command`: interpolated into the engineer prompt,
referenced nowhere else, and gating nothing since it shipped.

So this is a refusal at the tool boundary rather than a line of advice, in the
same spirit as `spec_contract.validate_refined_spec`: the agent is told exactly
what to add and retries, and a downstream scraper greps a guaranteed field
instead of a hoped-for one.

Two limits worth stating plainly, because a gate that cannot fail is worse than
no gate:

* **This cannot check that the measurement is true.** It checks that a claim of
  the right shape is present, and rejects the handful of phrasings that are
  known to assert nothing. An engineer determined to write a vacuous line in
  novel words will succeed.
* **It only reaches PRs this process can read.** The fetch is `gh`, so a GitLab
  MR is not checked -- see `_require_verify_line` in `minions/server/mcp.py`.
"""

import re

# A line whose first non-blank content is `VERIFY:`. Leading whitespace is
# allowed, as the spec requires -- an engineer that indents the line inside a
# list item has still written it, and burning a turn to teach it otherwise
# teaches nothing worth learning.
#
# Case-sensitive on purpose. `VERIFY:` is the token a downstream scraper greps
# for, so accepting `Verify:` here would hand that scraper a field it cannot
# find.
_VERIFY_LINE = re.compile(r"^[ \t]*VERIFY:[ \t]*(.*)$", re.MULTILINE)

# A continuation of the claim: an indented, non-blank line directly under the
# `VERIFY:` line. The worked example in the engineer prompt wraps across two
# physical lines, so a reader that stopped at the newline would see half a
# claim and call the other half missing.
_CONTINUATION = re.compile(r"^[ \t]+\S")

MISSING_VERIFY_ERROR = (
    "PR body does not contain a `VERIFY:` line — a single line naming a measurement "
    "that would come out different if the change did not work.\n"
    "\n"
    "Required syntax: VERIFY: <command or query> returns <expected outcome>. "
    "Before the fix <describe broken behavior>.\n"
    "\n"
    "See prompts/agents/engineer.md for full documentation and examples.\n"
    "\n"
    "If the change genuinely is not observable from outside, write "
    "`VERIFY: none — <why>` and say so in one line. That is a real finding and it "
    "is accepted; an absent line is not.\n"
    "\n"
    "Update the PR description, then call report_pr again."
)

# Phrasings that name the process rather than the outcome. Each says a job ran,
# not that the change worked -- and a gate nobody wired up reports green exactly
# like one that works, which is the failure this whole convention exists to
# catch.
#
# Matched only against the WHOLE claim, normalised. `VERIFY: tests pass` is
# refused; `VERIFY: tests pass — pytest tests/server/test_x.py goes from 0
# collected to 3 passed` is not, because the claim carries a measurement the
# list cannot match. That asymmetry is deliberate: a false refusal costs the
# agent a turn, so the check only fires where there is nothing else in the line.
EMPTY_VERIFY_ERROR = (
    "The PR body has a `VERIFY:` line with nothing after it. Name the measurement, "
    "or state `VERIFY: none — <why>` if the change genuinely is not observable from "
    "outside. An empty claim is indistinguishable from not having looked.\n"
    "\n"
    "Required syntax: VERIFY: <command or query> returns <expected outcome>. "
    "Before the fix <describe broken behavior>.\n"
    "\n"
    "See prompts/agents/engineer.md for worked examples.\n"
    "\n"
    "Update the PR description, then call report_pr again."
)

VACUOUS_CLAIMS = frozenset(
    {
        "ci is green",
        "ci passes",
        "ci passed",
        "the ci is green",
        "tests pass",
        "test pass",
        "tests passed",
        "tests passing",
        "all tests pass",
        "all tests passed",
        "all tests passing",
        "the tests pass",
        "tests pass locally",
        "all tests still pass",
        "lint clean",
        "lint is clean",
        "lint passes",
        "linting passes",
        "linting is clean",
        "see the diff",
        "see diff",
        "read the diff",
        "review the diff",
        "the build succeeds",
        "build succeeds",
        "the build passes",
        "build passes",
        "it works",
        "none",
    }
)


class PRVerificationError(ValueError):
    """Raised when a PR body does not satisfy the contract.

    Carries `remedy` -- the text handed back to the agent -- separately from the
    exception message, so callers can return it verbatim as a retryable error
    rather than re-wording it into something less actionable. Same shape as
    `SpecContractError`, and handled the same way at the tool boundary.
    """

    def __init__(self, remedy: str):
        self.remedy = remedy
        super().__init__(remedy)


def _vacuous_error(claim: str) -> str:
    return (
        f"The PR body's `VERIFY:` line says only {claim!r}, which names the process "
        "rather than the outcome. It says a job ran, not that the change worked — a "
        "gate nobody wired up reports green exactly like one that works.\n"
        "\n"
        "Replace it with a measurement someone else could run that would come out "
        "DIFFERENT if this change were reverted.\n"
        "\n"
        "Required syntax: VERIFY: <command or query> returns <expected outcome>. "
        "Before the fix <describe broken behavior>.\n"
        "\n"
        "If the change genuinely is not observable from outside, write "
        "`VERIFY: none — <why>` and explain in one line. An escape hatch with a "
        "reason is a finding; `VERIFY: none` with no reason is indistinguishable "
        "from not having looked.\n"
        "\n"
        "See prompts/agents/engineer.md for worked examples.\n"
        "\n"
        "Update the PR description, then call report_pr again."
    )


def _normalise(claim: str) -> str:
    """Lowercase, collapse whitespace, drop trailing sentence punctuation.

    So that `All tests pass.` and `all  tests   pass` are the same claim as far
    as the vacuity list is concerned. Without this the list would have to
    enumerate capitalisation and spacing, and would miss the first variant an
    agent actually wrote.
    """
    collapsed = " ".join((claim or "").split())
    return collapsed.strip().strip(".!").strip().lower()


def extract_verify_line(body: str) -> str | None:
    """The full `VERIFY:` claim including wrapped continuation lines, or None.

    Returns "" for a `VERIFY:` line with nothing after it -- a distinction the
    caller needs, because "there is no line" and "the line is empty" deserve the
    same refusal for different stated reasons.

    Lives here rather than in the caller so that what gets read back later is
    parsed by the same rule that decided what to accept. A second regex
    elsewhere drifts from this one, and then a body passes report_pr but scrapes
    as having no VERIFY line -- a contradiction with no obvious wrong party.
    """
    match = _VERIFY_LINE.search(body or "")
    if match is None:
        return None

    parts = [match.group(1).strip()]
    rest = (body or "")[match.end() :].lstrip("\n")
    for line in rest.split("\n"):
        if not _CONTINUATION.match(line):
            break
        parts.append(line.strip())

    return " ".join(p for p in parts if p).strip()


def validate_pr_body(body: str) -> None:
    """Raise PRVerificationError if the PR body carries no usable VERIFY: line."""
    claim = extract_verify_line(body)
    if claim is None:
        raise PRVerificationError(MISSING_VERIFY_ERROR)
    if not _normalise(claim):
        raise PRVerificationError(EMPTY_VERIFY_ERROR)
    if _normalise(claim) in VACUOUS_CLAIMS:
        raise PRVerificationError(_vacuous_error(claim))


def has_verify_line(body: str) -> bool:
    """True if the body carries a usable VERIFY: line. Never raises."""
    try:
        validate_pr_body(body)
        return True
    except PRVerificationError:
        return False
