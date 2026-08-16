"""The contract a refined spec must satisfy before it is accepted.

A refined spec replaces the raw one (see `update_job_spec`), and from that point
every downstream agent reads it as if it were the author's own words -- the
arbiter decomposes from it, engineers implement from it. Any gap the analyst
filled in silently becomes indistinguishable from something the human actually
asked for, and the PR reviewer has no way to tell the two apart.

So the assumptions section is required rather than encouraged. An analyst that
omits it is refused and told what to add, in the same spirit as
`InvalidTransitionError` naming its legal targets: a refusal that only says "no"
leaves an agent to guess, and agents guess badly.
"""

import re

# Matches a markdown heading (any level) whose text is "Assumptions", with or
# without trailing punctuation. Deliberately lenient about heading level and
# case: the point is to make the analyst state its guesses, and refusing a spec
# that says "### assumptions" would burn a turn teaching it nothing worth
# learning.
_ASSUMPTIONS_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*assumptions\b[:\s]*$", re.IGNORECASE | re.MULTILINE)

# Any markdown heading. Used to bound the assumptions section at the start of the
# next one. Without the bound, "is there text after the heading?" answers yes for
#
#     ## Assumptions
#
#     ## Notes
#     anything at all
#
# where the section is empty and the next section supplies the text -- so an empty
# section passed the very check whose error message is about empty sections.
_ANY_HEADING = re.compile(r"^\s{0,3}#{1,6}\s", re.MULTILINE)

MISSING_ASSUMPTIONS_ERROR = (
    "Refined spec is missing its `## Assumptions` section. Add it as the final "
    "section, listing every gap in the original ticket that you resolved: what "
    "was unspecified, how you read it, and why (a repo convention, the nearest "
    "analogous feature, or the most reversible default). If the ticket genuinely "
    "had no gaps, write exactly:\n\n## Assumptions\nNone — spec fully specified."
)

EMPTY_ASSUMPTIONS_ERROR = (
    "Refined spec has an `## Assumptions` heading with nothing under it. Either "
    "list the gaps you resolved, or state `None — spec fully specified.` if there "
    "were none. An empty section is indistinguishable from not having looked."
)


class SpecContractError(ValueError):
    """Raised when a refined spec does not satisfy the contract.

    Carries `remedy` -- the text handed back to the agent -- separately from the
    exception message, so callers can return it verbatim as a retryable error
    rather than re-wording it into something less actionable.
    """

    def __init__(self, remedy: str):
        self.remedy = remedy
        super().__init__(remedy)


def _section_body(spec: str) -> str | None:
    """The text under the assumptions heading, bounded by the next heading.

    Returns None when there is no heading at all, "" when the section is empty --
    a distinction the two error messages depend on.
    """
    match = _ASSUMPTIONS_HEADING.search(spec or "")
    if match is None:
        return None
    rest = spec[match.end() :]
    following = _ANY_HEADING.search(rest)
    if following is not None:
        rest = rest[: following.start()]
    return rest.strip()


def validate_refined_spec(spec: str) -> None:
    """Raise SpecContractError if the refined spec has no usable assumptions section."""
    body = _section_body(spec)
    if body is None:
        raise SpecContractError(MISSING_ASSUMPTIONS_ERROR)
    if not body:
        raise SpecContractError(EMPTY_ASSUMPTIONS_ERROR)


def has_assumptions(spec: str) -> bool:
    """True if the spec carries a non-empty assumptions section. Never raises."""
    try:
        validate_refined_spec(spec)
        return True
    except SpecContractError:
        return False


def extract_assumptions(spec: str) -> str:
    """Return the assumptions section's text, or "" if there is none.

    Lives here rather than in the grader so that what gets *read back* is parsed
    by the same rule that decided what to accept. A second regex elsewhere drifts
    from this one, and then a spec passes submission but grades as having no
    assumptions -- a contradiction with no obvious wrong party.
    """
    return _section_body(spec) or ""
