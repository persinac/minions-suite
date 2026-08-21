"""A reviewer that stops talking is not a reviewer that answered.

`_agent_loop_generic` used to break the moment a turn came back with no tool
calls. For a reviewer that wrote its review as prose instead of calling
`submit_review`, the run was recorded as a success with verdict=NULL -- 22 of
124 reviewer runs, 17.7%.

That is not cosmetic. `aggregate_verdicts` fails closed on a missing verdict, so
silence reads as an objection and the task is sent back for a revision round
nobody asked for. On job e180f866 all three rounds came from empty verdicts, with
zero comments and zero reviews on the PR -- a spurious round costs more than the
fan-out cap saves.

The prompt has said "You MUST call submit_review" all along
(prompts/agents/code_reviewer.md:17), so these tests are about ENFORCEMENT, not
wording. They drive the real loop against a fake `litellm.acompletion`, because
the bug lives in the loop's exit condition and a mocked loop would prove nothing.
"""

import json

import pytest
from litellm import ModelResponse
from litellm.types.utils import ChatCompletionMessageToolCall, Choices, Function, Message, Usage

from minions.agents import runner


def _prose(text: str = "Looks good to me overall.") -> ModelResponse:
    """A turn with content and NO tool calls — what ended the loop silently."""
    return ModelResponse(
        choices=[Choices(finish_reason="stop", index=0, message=Message(content=text, tool_calls=None, role="assistant"))],
        usage=Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        model="fake/test",
    )


def _tool(name: str, args: dict) -> ModelResponse:
    call = ChatCompletionMessageToolCall(id="call_1", type="function", function=Function(name=name, arguments=json.dumps(args)))
    return ModelResponse(
        choices=[Choices(finish_reason="tool_calls", index=0, message=Message(content=None, tool_calls=[call], role="assistant"))],
        usage=Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        model="fake/test",
    )


class _Executor:
    """Records what the loop actually invoked."""

    def __init__(self):
        self.calls: list[str] = []

    async def execute(self, name, args):
        self.calls.append(name)
        return json.dumps({"ok": True})


@pytest.fixture
def script(monkeypatch):
    """Queue of responses; the loop consumes one per turn."""
    queued: list[ModelResponse] = []
    seen_messages: list[list[dict]] = []

    async def fake_acompletion(**kwargs):
        seen_messages.append(list(kwargs.get("messages", [])))
        if not queued:
            return _prose("nothing left to say")
        return queued.pop(0)

    monkeypatch.setattr(runner.litellm, "acompletion", fake_acompletion)
    return {"queue": queued, "messages": seen_messages}


async def _run(tmp_path, _script, role, executor=None, max_turns=10):
    executor = executor or _Executor()
    return await runner._agent_loop_generic(
        model="fake/test",
        system_prompt="you are a reviewer",
        tools=[],
        tool_executor=executor,
        timeout=60,
        log_path=tmp_path / "agent.log",
        max_turns=max_turns,
        agent_role=role,
        agent_id="agent-1",
    )


class TestTheNudge:
    async def test_a_reviewer_that_stops_without_a_verdict_is_asked_again(self, tmp_path, script):
        """The core fix: prose is not a verdict, so the loop must not accept it."""
        executor = _Executor()
        script["queue"].extend([_prose(), _tool("submit_review", {"verdict": "approve", "body": "fine"}), _prose()])

        result = await _run(tmp_path, script, "code_reviewer", executor)

        assert result["verdict"] == "approve", "the nudge must recover a verdict the run would otherwise have lost"
        assert "submit_review" in executor.calls

    async def test_the_nudge_names_the_tool_and_says_silence_is_not_neutral(self, tmp_path, script):
        """A reviewer that thinks staying quiet is harmless will stay quiet."""
        script["queue"].extend([_prose(), _tool("submit_review", {"verdict": "approve", "body": "b"}), _prose()])

        await _run(tmp_path, script, "code_reviewer")

        injected = [m["content"] for turn in script["messages"] for m in turn if m.get("role") == "user"]
        nudge = "\n".join(str(c) for c in injected)
        assert "submit_review" in nudge
        assert "fails closed" in nudge, "it must say WHY silence is worse than a verdict"

    async def test_it_gives_up_rather_than_arguing_forever(self, tmp_path, script):
        """Bounded. Every extra round is a full turn of a metered agent, and the
        point is catching a forgotten call, not out-stubborning the model."""
        script["queue"].extend([_prose()] * 8)

        result = await _run(tmp_path, script, "code_reviewer")

        assert result["verdict"] is None
        user_turns = [m for turn in script["messages"] for m in turn if m.get("role") == "user" and "submit_review" in str(m.get("content", ""))]
        # The same message is re-sent in each subsequent request, so count the
        # distinct requests that carried at least one nudge rather than copies.
        carried = [t for t in script["messages"] if any("submit_review" in str(m.get("content", "")) for m in t if m.get("role") == "user")]
        assert user_turns, "it should have nudged at least once"
        assert len(carried) <= runner.MAX_FINAL_NUDGES + 1


class TestWhoGetsNudged:
    async def test_a_reviewer_that_already_submitted_is_left_alone(self, tmp_path, script):
        script["queue"].extend([_tool("submit_review", {"verdict": "request_changes", "body": "b"}), _prose()])

        await _run(tmp_path, script, "code_reviewer")

        nudged = any("submit_review" in str(m.get("content", "")) for turn in script["messages"] for m in turn if m.get("role") == "user")
        assert not nudged, "nagging an agent that already answered wastes a turn and invites a second verdict"

    async def test_an_engineer_is_not_nudged_for_a_verdict(self, tmp_path, script):
        """Engineers have no verdict to give. Their equivalent silent-stop bug
        (a stranded branch, no report_pr) has a different cause and fix."""
        script["queue"].extend([_prose()])

        result = await _run(tmp_path, script, "backend_engineer")

        assert result["verdict"] is None
        nudged = any("submit_review" in str(m.get("content", "")) for turn in script["messages"] for m in turn if m.get("role") == "user")
        assert not nudged

    @pytest.mark.parametrize("role", ["code_reviewer", "AgentRole.CODE_REVIEWER", "CODE_REVIEWER"])
    def test_the_role_is_matched_however_it_was_stringified(self, role):
        """It arrives as a str from several call sites, bare and enum-stringified."""
        assert runner._owes_final_call(role, None) is True

    def test_a_delivered_verdict_ends_the_obligation(self):
        assert runner._owes_final_call("code_reviewer", "approve") is False


class TestTheOtherVerdictTool:
    async def test_report_review_complete_also_counts(self, tmp_path, script):
        """A reviewer whose provider failed to build gets
        CODE_REVIEWER_TOOL_DEFINITIONS, which carries this tool as well. Its
        verdict used to be dropped on the floor -- losing a real answer, and now
        also badgering an agent that had already reported."""
        executor = _Executor()
        script["queue"].extend([_tool("report_review_complete", {"verdict": "approved", "feedback": "ok"}), _prose()])

        result = await _run(tmp_path, script, "code_reviewer", executor)

        assert result["verdict"] == "approved"
        nudged = any("submit_review" in str(m.get("content", "")) for turn in script["messages"] for m in turn if m.get("role") == "user")
        assert not nudged

    async def test_submit_review_wins_when_both_are_called(self, tmp_path, script):
        """submit_review is the one that reaches the git provider."""
        script["queue"].extend(
            [
                _tool("submit_review", {"verdict": "request_changes", "body": "b"}),
                _tool("report_review_complete", {"verdict": "approved", "feedback": "f"}),
                _prose(),
            ]
        )

        result = await _run(tmp_path, script, "code_reviewer")

        assert result["verdict"] == "request_changes"


class TestHardStopIsRoleAware:
    def test_a_reviewer_is_told_to_submit_not_to_push(self):
        """The single engineer-shaped message told reviewers to "commit and push
        ... then create the merge request" -- work they must not do -- and never
        mentioned the one call that ends a review well."""
        msg = runner._hard_stop_instruction("code_reviewer")

        assert "submit_review" in msg
        assert "commit" not in msg
        assert "merge request" not in msg

    def test_an_engineer_still_gets_the_shipping_instruction(self):
        msg = runner._hard_stop_instruction("backend_engineer")

        assert "report_pr" in msg
        assert "submit_review" not in msg

    def test_every_tool_named_is_one_the_loop_would_allow(self):
        """A hard-stop that names a BLOCKED tool sends the agent into a wall."""
        import re

        allowed = {
            "create_branch",
            "commit",
            "push",
            "create_pr",
            "report_pr",
            "complete_subtask",
            "fail_subtask",
            "update_task_status",
            "send_heartbeat",
            "submit_review",
            "post_inline_comment",
            "report_review_complete",
        }
        for role in ("code_reviewer", "backend_engineer"):
            named = set(re.findall(r"\b(?:[a-z]+_)+[a-z]+\b", runner._hard_stop_instruction(role)))
            assert named <= allowed, f"{role} is told to call something the hard stop blocks: {named - allowed}"


class TestTheseTestsGoRedWithoutTheFix:
    """Mutation check: disable the nudge and the verdict must be lost again.

    Green proves nothing on its own here — the pre-fix loop also "passed", it
    just returned NULL and let the gate fail closed. This pins that the recovery
    is caused by the nudge and not by the fake happening to call submit_review.
    """

    async def test_with_nudging_disabled_the_verdict_is_lost(self, tmp_path, script, monkeypatch):
        monkeypatch.setattr(runner, "MAX_FINAL_NUDGES", 0)
        executor = _Executor()
        script["queue"].extend([_prose(), _tool("submit_review", {"verdict": "approve", "body": "fine"}), _prose()])

        result = await _run(tmp_path, script, "code_reviewer", executor)

        assert result["verdict"] is None, "without the nudge this run ends silently — that was the bug"
        assert "submit_review" not in executor.calls, "the loop never gave it the chance to submit"
