"""A PR body must carry a VERIFY: line, and it must say something.

`prompts/agents/engineer.md` has instructed engineers to end the PR body with a
`VERIFY:` line since it was written. Nothing read the body back, so the
convention gated nothing -- the same shape as `service.test_command`, which is
interpolated into the engineer prompt, referenced nowhere else in `minions/`,
and has gated nothing since it shipped.

Three properties are pinned here:

* **Presence is required.** A body with no line is refused with the remediation
  text verbatim, so the agent is told what to add rather than merely told no.
* **The escape hatch survives.** The prompt tells an engineer who cannot name a
  measurement to write `VERIFY: none — <why>`. If this validator refused that,
  the gate and the prompt the agent actually reads would contradict each other,
  and the agent would be refused for doing as it was told.
* **A claim that names only the process is refused.** "CI is green" says a job
  ran, not that the change worked. This is the narrow half of the check and it
  is honest about being narrow: it matches the WHOLE claim against a list of
  known-empty phrasings, so it catches the forms the prompt already names and
  nothing cleverer.

What would make these wrong: if `validate_pr_body` were changed to accept
anything non-empty, every test in `TestRefusal` would fail. If it were changed
to reject the escape hatch, `test_the_documented_escape_hatch_is_accepted`
would fail. Both directions are reachable.
"""

import pytest

from minions.core.pr_contract import (
    MISSING_VERIFY_ERROR,
    PRVerificationError,
    extract_verify_line,
    has_verify_line,
    validate_pr_body,
)

# The remediation text the spec requires, verbatim. Pinned as a literal rather
# than imported so that rewording the module's message fails this test instead
# of silently redefining what the contract promised.
REQUIRED_REMEDIATION = (
    "PR body does not contain a `VERIFY:` line — a single line naming a measurement "
    "that would come out different if the change did not work.\n"
    "\n"
    "Required syntax: VERIFY: <command or query> returns <expected outcome>. "
    "Before the fix <describe broken behavior>.\n"
    "\n"
    "See prompts/agents/engineer.md for full documentation and examples."
)


class TestRefusal:
    def test_a_body_with_no_verify_line_is_refused(self):
        with pytest.raises(PRVerificationError) as exc:
            validate_pr_body("## Summary\n\nFixes the thing.\n")

        assert exc.value.remedy == MISSING_VERIFY_ERROR

    def test_an_empty_body_is_refused(self):
        with pytest.raises(PRVerificationError):
            validate_pr_body("")

    def test_the_refusal_carries_the_exact_remediation_text(self):
        """AC3: the message must name the fix and point at the documentation."""
        with pytest.raises(PRVerificationError) as exc:
            validate_pr_body("no line here")

        assert REQUIRED_REMEDIATION in exc.value.remedy
        assert "prompts/agents/engineer.md" in exc.value.remedy

    def test_the_remedy_is_carried_separately_from_the_message(self):
        """Same shape as SpecContractError, so the tool boundary can hand it
        back verbatim rather than re-wording it into something less actionable."""
        err = PRVerificationError("do the thing")

        assert err.remedy == "do the thing"
        assert str(err) == "do the thing"

    def test_a_verify_keyword_with_nothing_after_it_is_refused(self):
        with pytest.raises(PRVerificationError) as exc:
            validate_pr_body("body\nVERIFY:\n")

        assert "nothing after it" in exc.value.remedy

    def test_lowercase_verify_does_not_count(self):
        """`VERIFY:` is the token a downstream scraper greps for. Accepting
        `verify:` would hand that scraper a field it cannot find."""
        with pytest.raises(PRVerificationError):
            validate_pr_body("body\nverify: something real and measurable\n")

    @pytest.mark.parametrize(
        "claim",
        [
            "CI is green",
            "ci is green.",
            "tests pass",
            "All tests pass.",
            "all tests passing",
            "lint clean",
            "linting passes",
            "see the diff",
            "the build succeeds",
            "it works",
        ],
    )
    def test_a_claim_that_names_only_the_process_is_refused(self, claim):
        """These say a job ran, not that the change worked. A gate nobody
        wired up reports green exactly like one that works."""
        with pytest.raises(PRVerificationError) as exc:
            validate_pr_body(f"## Summary\n\nstuff\n\nVERIFY: {claim}\n")

        assert "process" in exc.value.remedy

    def test_a_bare_none_with_no_reason_is_refused(self):
        """The prompt asks for `none — <why>`. A reason-free escape hatch is
        indistinguishable from not having looked, exactly as an empty
        assumptions section is in spec_contract."""
        with pytest.raises(PRVerificationError):
            validate_pr_body("body\nVERIFY: none\n")


class TestAcceptance:
    def test_a_real_measurement_is_accepted(self):
        validate_pr_body("## Summary\n\nstuff\n\nVERIFY: `GET /health` returns a build_sha field. It returned 404 before.\n")

    def test_the_documented_escape_hatch_is_accepted(self):
        """`prompts/agents/engineer.md` tells an engineer who cannot name a
        measurement to write exactly this. A gate that refused it would punish
        an agent for obeying the prompt it was given."""
        validate_pr_body("body\n\nVERIFY: none — the change is a comment edit with no runtime surface.\n")

    def test_leading_whitespace_is_allowed(self):
        """Assumption 4. An engineer that indents the line inside a list item
        has still written it."""
        validate_pr_body("body\n\n  VERIFY: pytest tests/a.py goes from 0 collected to 3 passed.\n")

    def test_a_process_word_inside_a_real_claim_is_not_refused(self):
        """The vacuity list matches the WHOLE claim. A false refusal costs the
        agent a turn, so the check only fires where there is nothing else."""
        validate_pr_body("body\n\nVERIFY: tests pass — specifically pytest tests/server/test_x.py goes from 0 collected to 3 passed.\n")

    def test_a_line_anywhere_in_the_body_counts(self):
        validate_pr_body("VERIFY: the counter reads 15, not 0.\n\n## Summary\n\nstuff\n")


class TestWrappedClaims:
    """Assumption 5: the claim may wrap across physical lines. A reader that
    stopped at the first newline would see half a claim and could call the
    other half missing."""

    def test_an_indented_continuation_joins_the_claim(self):
        body = "VERIFY: on master, npx tsc --noEmit -p tsconfig.web.json --listFiles\n        | grep -c src/renderer returns 15.\n"

        assert extract_verify_line(body) == "on master, npx tsc --noEmit -p tsconfig.web.json --listFiles | grep -c src/renderer returns 15."

    def test_a_blank_line_ends_the_claim(self):
        body = "VERIFY: the counter reads 15.\n\nUnrelated closing paragraph.\n"

        assert extract_verify_line(body) == "the counter reads 15."

    def test_an_unindented_next_line_ends_the_claim(self):
        body = "VERIFY: the counter reads 15.\nUnrelated closing paragraph.\n"

        assert extract_verify_line(body) == "the counter reads 15."

    def test_no_line_at_all_returns_none(self):
        """Distinct from "" -- "there is no line" and "the line is blank" are
        different mistakes and get different instructions."""
        assert extract_verify_line("nothing here") is None
        assert extract_verify_line("VERIFY:") == ""


class TestHasVerifyLine:
    def test_it_never_raises(self):
        assert has_verify_line("VERIFY: a real measurement of something") is True
        assert has_verify_line("") is False
        assert has_verify_line("VERIFY: tests pass") is False
