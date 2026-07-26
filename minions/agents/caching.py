"""Prompt caching for the agent tool-use loop.

The measured job f6451f44 spent $20.57 on one engineer over 64 turns — 3.85M
input tokens against 53k output. 94% of the cost was input, because an agentic
loop re-sends its entire prefix on every turn: turn N carries turns 1..N-1.

Anthropic caches that prefix at 1.25x to write and 0.1x to read, so the same
workload projects to roughly a third. Nothing about the agent's behaviour
changes — it is the identical conversation, billed differently.

Two things make this less trivial than it sounds:

* **Breakpoints are limited.** Anthropic allows at most 4 `cache_control`
  markers. Marking every message would be rejected, so the strategy is a static
  breakpoint on the system prompt plus a rolling one near the end of history,
  which extends the cached prefix as the conversation grows.
* **Not every model supports it.** Kimi does not
  (`supports_prompt_caching` is False), and sending `cache_control` to a model
  that does not understand it risks a 400 rather than a silent ignore. The
  transform is therefore gated on the model, not applied unconditionally —
  which matters now that MODEL_ENGINEER can point at another vendor.

There is also a floor: Anthropic will not cache a prefix below ~1024 tokens
(2048 for Haiku). Short conversations simply see no cache hits, which is
correct, not a failure.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)

CACHE_CONTROL = {"type": "ephemeral"}

# Anthropic's hard limit. Exceeding it is an API error, not a degradation.
MAX_BREAKPOINTS = 4


def supports_caching(model: str) -> bool:
    """Whether this model understands cache_control markers."""
    if not model:
        return False
    try:
        from litellm.utils import supports_prompt_caching

        return bool(supports_prompt_caching(model=model))
    except Exception as e:
        # An unrecognised model is not a reason to fail the turn; it just does
        # not get cached.
        logger.debug("Could not determine caching support for %r: %s", model, e)
        return False


def _as_blocks(content: Any) -> list[dict] | None:
    """Normalise message content to a content-block list, or None if unsupported."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [dict(block) for block in content if isinstance(block, dict)]
    return None


def _mark(message: dict) -> dict:
    """Return a copy of `message` with cache_control on its final content block."""
    blocks = _as_blocks(message.get("content"))
    if not blocks:
        return message

    marked = dict(message)
    blocks[-1] = {**blocks[-1], "cache_control": CACHE_CONTROL}
    marked["content"] = blocks
    return marked


def apply_cache_control(messages: list[dict], model: str) -> list[dict]:
    """Return `messages` with cache breakpoints, or unchanged if unsupported.

    Two breakpoints:

    1. The system prompt — identical on every turn, and it carries the persona
       and tool instructions, so it is the largest guaranteed-stable block.
    2. The last message of the prior turn — a rolling marker, so each turn
       extends the cached prefix rather than only ever caching the system
       prompt. Without it, everything the agent has read and done is re-charged
       at full price forever, which on a 64-turn run is most of the bill.

    Never mutates the input: the caller keeps appending to its own list, and a
    stale cache_control left on an interior message would consume one of the
    four breakpoints permanently.
    """
    if not messages or not supports_caching(model):
        return messages

    out = [dict(m) for m in messages]
    used = 0

    for i, message in enumerate(out):
        if message.get("role") == "system":
            out[i] = _mark(message)
            used += 1
            break

    # Roll a breakpoint onto the last message that is not the one we are about
    # to answer. Marking the final message would cache a prefix that includes
    # content only ever sent once.
    if len(out) > 2 and used < MAX_BREAKPOINTS:
        tail = len(out) - 2
        if out[tail].get("role") != "system":
            out[tail] = _mark(out[tail])

    return out


def extract_cache_tokens(response: Any) -> tuple[int, int]:
    """(cache_read, cache_creation) from a completion response.

    Both zero when caching did not engage — an unsupported model, or a prefix
    below Anthropic's ~1024-token floor. Recording them is what makes the
    difference visible: minions has had the DB columns since the beginning and
    wrote 0 to them on every agent ever run, which is precisely why nobody
    noticed caching was off.
    """
    usage = getattr(response, "usage", None)
    if not usage:
        return 0, 0

    details = getattr(usage, "prompt_tokens_details", None)
    if not details:
        return 0, 0

    read = getattr(details, "cached_tokens", 0) or 0
    created = getattr(details, "cache_creation_tokens", 0) or 0
    return int(read), int(created)
