"""The engineer's workflow must degrade, not destroy, when budget runs out.

Job dbc956ff exhausted its turn budget at $0.93 of an $8 ceiling — it ran out of
turns, not money — with the code changes already written and nothing committed.
The prompt put `create_branch` at step 4 and `commit` at step 5, *after* every
subtask, so an agent that ran long had its entire output discarded and the job
restarted from zero. Another run spent 2.6M input tokens on a 3-file change.

Two properties fix that, and both are prompt-level:

* branch first, commit per subtask — so a turn-exhausted agent leaves committed
  work on a pushed branch instead of an unrecoverable dirty tree
* an explicit subtask cap — 7-8 subtask plans consistently overran on tasks the
  5-subtask plans finished comfortably

These are asserted rather than left to review because the file is prose: a
well-meaning edit that reorders the workflow reintroduces the bug silently, and
nothing else in the suite would notice.
"""

from pathlib import Path

import pytest

PROMPT = Path(__file__).resolve().parent.parent / "prompts" / "agents" / "engineer.md"


@pytest.fixture(scope="module")
def text() -> str:
    return PROMPT.read_text()


class TestWorkDegradesGracefully:
    def test_the_branch_is_created_before_any_code_is_written(self, text):
        branch = text.index("create_branch")
        plan = text.index("submit_subtask_plan")
        implement = text.index("**Implement**")

        assert branch < plan < implement, "create_branch must come before planning and implementation"

    def test_it_commits_between_subtasks(self, text):
        """Without this, everything since the last subtask dies with the agent."""
        assert "`commit` before moving to the next subtask" in text

    def test_the_reason_is_stated_not_just_the_rule(self, text):
        """An agent that knows why will hold the property under pressure; one
        following a checklist will reorder it the moment a step seems faster."""
        assert "everything you did is lost" in text
        assert "Branch early, commit often." in text


class TestBudgetIsBounded:
    def test_the_subtask_cap_is_explicit(self, text):
        assert "5 subtasks or fewer" in text

    def test_the_cap_has_an_escape_hatch(self, text):
        """A hard cap with no way out invites the agent to lie about scope."""
        assert "update_task_status" in text.split("## Budget")[1].split("##")[0]

    def test_reading_is_bounded(self, text):
        budget = text.split("## Budget")[1]
        assert "Read narrowly" in budget
        assert "do not re-read a file you have already seen" in budget

    def test_test_runs_are_bounded(self, text):
        assert "narrowest test command" in text

    def test_blocked_tools_are_called_out_as_a_turn_sink(self, text):
        """Each rejected call still costs a turn — an agent that keeps retrying
        during hard-stop spends its whole wrap-up window on rejections."""
        assert "burns the very turns you need" in text


class TestWorkflowStillComplete:
    @pytest.mark.parametrize(
        "tool",
        ["create_branch", "submit_subtask_plan", "start_subtask", "complete_subtask", "commit", "push", "create_pr", "report_pr"],
    )
    def test_every_required_tool_is_still_named(self, text, tool):
        """Trimming the prompt for budget must not drop a step from the path."""
        assert tool in text

    def test_branch_naming_is_given_exactly_once(self, text):
        """It moved into step 2; leaving the old copy in Implementation
        Guidelines gives two sources of truth for the same convention."""
        assert text.count("feat/job-<job-id>/<slug>") == 1
