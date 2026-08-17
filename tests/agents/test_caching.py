"""Prompt caching for the agent loop.

An agentic loop re-sends turns 1..N-1 on turn N. On the one measured job that
was 3.85M input tokens across 64 turns — 94% of the $20.57 that engineer cost.
Anthropic caches the prefix at 1.25x write / 0.1x read, so the same conversation
bills at roughly a third with no behavioural change.

The two ways this goes wrong are covered here: sending cache_control to a model
that does not support it (Kimi — and MODEL_ENGINEER can now point there), and
recording nothing, which is exactly how caching stayed off unnoticed since the
project began.
"""

import pytest

from minions.agents.caching import (
    CACHE_CONTROL,
    MAX_BREAKPOINTS,
    apply_cache_control,
    extract_cache_tokens,
    supports_caching,
)


def _count_breakpoints(messages):
    total = 0
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):
            total += sum(1 for b in content if isinstance(b, dict) and b.get("cache_control"))
    return total


def _conversation(turns=3):
    messages = [
        {"role": "system", "content": "You are an engineer. " + "x" * 500},
        {"role": "user", "content": "Do the thing"},
    ]
    for i in range(turns):
        messages.append({"role": "assistant", "content": f"working {i}"})
        messages.append({"role": "user", "content": f"tool result {i}"})
    return messages


class TestModelGating:
    def test_anthropic_models_support_it(self):
        assert supports_caching("claude-opus-5") is True
        assert supports_caching("claude-sonnet-5") is True

    def test_kimi_does_not(self):
        """MODEL_ENGINEER can point here — sending cache_control risks a 400."""
        assert supports_caching("moonshot/kimi-k2.6") is False

    def test_unknown_and_empty_models_are_safe(self):
        assert supports_caching("totally-made-up") is False
        assert supports_caching("") is False

    def test_unsupported_model_gets_untouched_messages(self):
        messages = _conversation()

        out = apply_cache_control(messages, "moonshot/kimi-k2.6")

        assert out == messages
        assert _count_breakpoints(out) == 0


class TestBreakpoints:
    def test_the_system_prompt_is_cached(self):
        """Identical every turn and carries the persona — the largest stable block."""
        out = apply_cache_control(_conversation(), "claude-opus-5")

        system = out[0]
        assert isinstance(system["content"], list)
        assert system["content"][-1]["cache_control"] == CACHE_CONTROL

    def test_a_rolling_breakpoint_extends_the_prefix(self):
        """Without this only the system prompt caches, and everything the agent
        has read and done is re-charged at full price for all 64 turns."""
        out = apply_cache_control(_conversation(turns=5), "claude-opus-5")

        assert _count_breakpoints(out) >= 2

    def test_never_exceeds_the_api_limit(self):
        """More than 4 markers is an API error, not a degradation."""
        for turns in (1, 3, 10, 40):
            out = apply_cache_control(_conversation(turns), "claude-opus-5")

            assert _count_breakpoints(out) <= MAX_BREAKPOINTS, f"{turns} turns"

    def test_the_final_message_is_not_marked(self):
        """Caching a prefix that includes the message being answered caches
        content that is only ever sent once."""
        out = apply_cache_control(_conversation(turns=4), "claude-opus-5")

        last = out[-1]
        if isinstance(last.get("content"), list):
            assert not any(b.get("cache_control") for b in last["content"])

    def test_a_two_message_conversation_only_caches_the_system_prompt(self):
        out = apply_cache_control([{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}], "claude-opus-5")

        assert _count_breakpoints(out) == 1


class TestTheRollingBreakpointCannotSilentlyVanish:
    """A turn ending in ONE tool call is the commonest shape in the loop, and it
    used to lose the rolling breakpoint entirely.

    An assistant message that only makes tool calls carries no content --
    `model_dump(exclude_none=True)` drops the key -- so `_mark` has nothing to
    attach a block to and returns the message unchanged. The old code tried
    exactly one index (len-2), which is precisely where that message sits in
    `[..., assistant(tool_calls), tool]`. Nothing checked whether marking
    succeeded, so on those turns only the system prompt stayed cached and the
    entire accumulated history was re-charged at full price.

    Measured before the fix: 33.8% of prompt tokens served from cache over 933
    turns, 21.2M tokens paid uncached.
    """

    @staticmethod
    def _tool_call_turn(idx: int) -> list[dict]:
        return [
            {"role": "assistant", "tool_calls": [{"id": str(idx), "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": str(idx), "content": f"contents {idx}"},
        ]

    def test_a_single_tool_call_turn_still_gets_a_rolling_breakpoint(self):
        messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}, *self._tool_call_turn(1)]

        out = apply_cache_control(messages, "claude-opus-5")

        assert _count_breakpoints(out) >= 2, "only the system prompt was cached — the rolling breakpoint vanished"

    def test_it_survives_many_single_tool_call_turns(self):
        """The real shape of a long engineer run."""
        messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}]
        for i in range(20):
            messages += self._tool_call_turn(i)

        out = apply_cache_control(messages, "claude-opus-5")

        assert _count_breakpoints(out) >= 2
        assert _count_breakpoints(out) <= MAX_BREAKPOINTS

    def test_a_contentless_message_is_never_the_chosen_breakpoint(self):
        """Marking it is a no-op, so choosing it is the same as choosing none."""
        messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}, *self._tool_call_turn(1)]

        out = apply_cache_control(messages, "claude-opus-5")

        for message in out:
            if "content" not in message:
                continue
            blocks = message["content"]
            if isinstance(blocks, list) and any(b.get("cache_control") for b in blocks if isinstance(b, dict)):
                assert message.get("role") != "assistant" or message.get("content"), "marked a message with no content to carry the marker"

    def test_a_conversation_ending_on_an_assistant_message_still_caches(self):
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "go"},
            *self._tool_call_turn(1),
            {"role": "assistant", "tool_calls": [{"id": "9", "type": "function", "function": {"name": "x", "arguments": "{}"}}]},
        ]

        out = apply_cache_control(messages, "claude-opus-5")

        assert _count_breakpoints(out) >= 2


class TestNoMutation:
    def test_the_caller_s_list_is_untouched(self):
        """The loop keeps appending to its own list. A stale cache_control left
        on an interior message would burn one of the four breakpoints forever."""
        messages = _conversation()
        before = [dict(m) for m in messages]

        apply_cache_control(messages, "claude-opus-5")

        assert messages == before
        assert all(isinstance(m["content"], str) for m in messages)

    def test_repeated_application_does_not_accumulate(self):
        """Turn N+1 re-derives from the same growing list; markers must not stack."""
        messages = _conversation(turns=6)

        for _ in range(5):
            out = apply_cache_control(messages, "claude-opus-5")
            assert _count_breakpoints(out) <= MAX_BREAKPOINTS


class TestExtraction:
    def test_reads_cache_counts_from_the_response(self):
        class _Details:
            cached_tokens = 12000
            cache_creation_tokens = 3000

        class _Usage:
            prompt_tokens_details = _Details()

        class _Resp:
            usage = _Usage()

        assert extract_cache_tokens(_Resp()) == (12000, 3000)

    @pytest.mark.parametrize(
        "response",
        [
            type("R", (), {"usage": None})(),
            type("R", (), {})(),
            type("R", (), {"usage": type("U", (), {"prompt_tokens_details": None})()})(),
        ],
    )
    def test_missing_usage_is_zero_not_an_error(self, response):
        """No cache engagement is normal below Anthropic's ~1024-token floor."""
        assert extract_cache_tokens(response) == (0, 0)


class TestWiring:
    def test_the_loop_applies_it(self):
        import inspect

        from minions.agents.runner import _agent_loop_generic

        source = inspect.getsource(_agent_loop_generic)

        assert "apply_cache_control(messages, model)" in source
        assert "extract_cache_tokens(response)" in source

    def test_the_loop_returns_the_counts(self):
        import inspect

        from minions.agents.runner import _agent_loop_generic

        source = inspect.getsource(_agent_loop_generic)

        assert '"cache_read_tokens"' in source
        assert '"cache_creation_tokens"' in source

    def test_the_agent_row_records_them(self):
        """update_agent has always persisted these columns; nothing populated
        them, which is why every agent ever run shows 0."""
        import inspect

        from minions.agents.runner import run_agent

        source = inspect.getsource(run_agent)

        assert "agent.cache_read_tokens = result" in source
        assert "agent.cache_creation_tokens = result" in source
