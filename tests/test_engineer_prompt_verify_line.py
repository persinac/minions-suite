"""The prompt's VERIFY: examples must agree with the gate that enforces them.

`report_pr` now refuses a PR body with no usable `VERIFY:` line. That makes
`prompts/agents/engineer.md` and `minions/core/pr_contract.py` two statements of
one rule, and two statements of one rule drift.

The specific way they drift is cheap to imagine and expensive to hit: someone
adds a `Bad —` example to the prompt that the validator happily accepts, so an
agent is told a form is wrong, avoids it, and would not have been refused for
using it. Or worse, a `Good —` example the validator refuses -- the agent does
exactly as instructed, `report_pr` says no, and the agent has no way to tell
which of the two sources is lying.

So the examples are executed rather than read. Every `Good —` line in the prompt
is fed to the validator and must be accepted; every `Bad —` line must be
refused. Adding an example to the prompt adds a test case here automatically.

What would make this wrong: if the parser matched nothing, the assertions would
vacuously pass -- a test that cannot fail is the exact failure mode this suite
keeps finding elsewhere. `test_the_examples_were_actually_found` is the floor
that stops that.
"""

import re
from pathlib import Path

import pytest

from minions.core.pr_contract import has_verify_line

PROMPT = Path(__file__).resolve().parent.parent / "prompts" / "agents" / "engineer.md"

# - Good — `VERIFY: the test job prints "collected: 17". Before this PR it printed 0.`
_EXAMPLE = re.compile(r"^- (Good|Bad) — `(VERIFY:.*)`\s*$", re.MULTILINE)


@pytest.fixture(scope="module")
def text() -> str:
    return PROMPT.read_text()


@pytest.fixture(scope="module")
def examples(text) -> list[tuple[str, str]]:
    return _EXAMPLE.findall(text)


class TestExamplesMatchTheGate:
    def test_the_examples_were_actually_found(self, examples):
        """Without this floor, a parser that matched nothing would let every
        other assertion in this file pass while checking nothing at all."""
        verdicts = {verdict for verdict, _ in examples}

        assert len(examples) >= 6, f"expected the prompt's worked examples, parsed {len(examples)}"
        assert verdicts == {"Good", "Bad"}, f"expected both verdicts, got {verdicts}"

    def test_every_good_example_is_accepted(self, examples):
        """An agent that copies a `Good —` line must not then be refused."""
        rejected = [claim for verdict, claim in examples if verdict == "Good" and not has_verify_line(claim)]

        assert rejected == [], f"the prompt calls these good but the gate refuses them: {rejected}"

    def test_every_bad_example_is_refused(self, examples):
        """A `Bad —` line the gate accepts is advice the gate does not back."""
        accepted = [claim for verdict, claim in examples if verdict == "Bad" and has_verify_line(claim)]

        assert accepted == [], f"the prompt calls these bad but the gate accepts them: {accepted}"


class TestTheRuleIsStatedAsEnforced:
    def test_the_prompt_says_report_pr_will_refuse(self, text):
        """An agent that believes the line is optional will omit it and burn a
        turn discovering otherwise. The prompt is where that turn is saved."""
        assert "`report_pr` will refuse" in text

    def test_the_refusal_is_described_as_retryable(self, text):
        assert "retryable" in text

    def test_the_escape_hatch_is_still_documented(self, text):
        """The gate accepts `VERIFY: none — <why>`. If the prompt stopped
        offering it, an engineer with a genuinely unobservable change would
        have no accepted way to say so."""
        assert "VERIFY: none — <why>" in text

    def test_the_documented_escape_hatch_is_one_the_gate_accepts(self):
        """Executed, not asserted about: the prompt's exact escape hatch, filled
        in, must survive the validator."""
        assert has_verify_line("VERIFY: none — the change is a comment edit with no runtime surface.") is True
        assert has_verify_line("VERIFY: none") is False
