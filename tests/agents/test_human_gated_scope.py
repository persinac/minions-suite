"""Work gated on a human credential must be declined, not wrapped in a script.

Job f7e0563f (2026-08-30) asked for a deletion-protection Deny on a KMS key. The
key was in no IaC, so the engineer wrote a script, a test and a notes file — 607
lines — argued carefully for why a script was the right shape, and got two
approvals and an auto-merge. The operation then failed for a reason none of that
anticipated, and the risk the ticket was filed to close is still open.

Nothing in the loop could catch it. The claim was only falsifiable by running the
script against real AWS, which no reviewer did, and the job is legitimately
`done` — so neither the review panel nor `real_failed` would ever have flagged
it. The only place to stop it is before it is built.

Two halves, and the second is the one that can rot: the engineer prompt states
the rule, and `report_no_work_needed` has to ACCEPT that case. If the tool
description narrows back to "already present", the prompt is instructing an agent
to call a tool that tells it not to — and the model resolves that by building the
script anyway.
"""

from pathlib import Path

import pytest

from minions.agents.tools.definitions import get_tools_for_role

PROMPTS = Path(__file__).resolve().parents[2] / "prompts"


def _tool_description(role: str, name: str) -> str:
    for tool in get_tools_for_role(role):
        if tool["function"]["name"] == name:
            return tool["function"]["description"].lower()
    raise AssertionError(f"{role} has no {name} tool")


class TestEngineerPrompt:
    def _text(self) -> str:
        return (PROMPTS / "agents" / "engineer.md").read_text().lower()

    def test_it_states_the_rule_and_names_the_trigger(self):
        text = self._text()
        assert "mfa" in text, "the rule must name the concrete trigger, not just gesture at 'privileged'"
        assert "report_no_work_needed" in text, "a rule with no mechanism is a suggestion"

    def test_it_forbids_the_shape_that_actually_shipped(self):
        """The failure was not idleness — it was 607 plausible lines."""
        text = self._text()
        assert "script" in text, "the prompt must name the script-wrapper shape it is ruling out"

    def test_it_judges_the_deliverable_not_the_subject(self):
        """Without this the rule reads as 'never touch AWS' and blocks ordinary work."""
        text = self._text()
        assert "deliverable" in text


class TestSpecAnalystPrompt:
    def test_the_analyst_flags_it_too(self):
        """Catching it at the spec costs one cheap agent instead of an engineer and two reviewers."""
        text = (PROMPTS / "agents" / "spec_analyst.md").read_text().lower()
        assert "mfa" in text
        assert "scope" in text, "the flag has to land somewhere the engineer reads"


@pytest.mark.parametrize("role", ["backend_engineer", "frontend_engineer"])
class TestToolContract:
    def test_the_tool_accepts_the_case_the_prompt_sends_it(self, role):
        """Otherwise prompt and tool contradict, and the model picks one."""
        desc = _tool_description(role, "report_no_work_needed")
        assert "mfa" in desc, "report_no_work_needed must admit the human-gated case"

    def test_it_is_not_a_general_give_up_hatch(self, role):
        """The guard that keeps this from becoming 'bail on anything hard'.

        This is the risk in widening a terminal, no-PR escape: a task that is
        merely difficult, or blocked on something that will later clear, must
        still be attempted. Case (2) is a permanent limit on the agent.
        """
        desc = _tool_description(role, "report_no_work_needed")
        assert "hard" in desc, "the 'not merely hard' guard must survive"
        assert "permanent" in desc, "the permanent-vs-temporary distinction is what bounds the hatch"

    def test_it_still_requires_checkable_evidence(self, role):
        """An unexplained 'cannot' closes a task terminally — make the claim auditable."""
        for tool in get_tools_for_role(role):
            if tool["function"]["name"] == "report_no_work_needed":
                params = tool["function"]["parameters"]["properties"]
                assert "reason" in params
                assert "mfa" in params["reason"]["description"].lower(), (
                    "the reason field must show what a case-(2) answer looks like, or agents supply case-(1) shaped evidence for it"
                )
