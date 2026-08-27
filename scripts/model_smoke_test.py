#!/usr/bin/env python3
"""Decide whether a model can survive the agent loop, before it gets a tier.

Exists because the dangerous failure is silent. `_agent_loop_generic` ends at
`runner.py:563` on `finish_reason == "stop" or not message.tool_calls`, and that
one condition cannot tell "I finished the work" from "I never called anything
because I can't". A model that does not emit the tool schema writes prose
describing what it would do, the loop reads the empty `tool_calls` as delivery,
and the run is recorded a success. Nothing raises. The text is plausible,
because a capable model narrates exactly the right steps -- it just took none
of them.

That already happened here with a model that CAN call tools: 22 of 124 reviewer
runs finished with verdict=NULL (see the comment at runner.py:564). A model that
cannot emit the schema hits that exit on turn one, every time. So the check that
matters is not "does it answer" -- it is "does it answer with a tool call".

The call below mirrors `runner.py:508` exactly: same tools, same max_tokens,
same timeout, and NO tool_choice. Forcing tool_choice would prove the model can
be made to call a tool, which is not the question -- production never forces it.

Nothing is executed. Tool calls are inspected and discarded, so this is safe to
point at a role whose schema contains write_file, commit and push.

Usage:
    NANOGPT_API_KEY=... uv run python scripts/model_smoke_test.py nano-gpt/<id>
    MOONSHOT_API_KEY=... uv run python scripts/model_smoke_test.py moonshot/kimi-k2.7-code
    uv run python scripts/model_smoke_test.py <model> --role code_reviewer

Exit code is 0 only if every required check passes, so it can gate a rollout.
"""

import argparse
import asyncio
import json
import sys

PASS = "PASS"
FAIL = "FAIL"
WARN = "WARN"

# A task that cannot be answered from the model's own knowledge. It has no way
# to know what is in the diff, so the only honest completion is a tool call.
# Deliberately not phrased as "call get_mr_diff" -- being told which function to
# invoke tests instruction-following, and what we need to know is whether the
# model reaches for the schema on its own, the way it will in production.
PROBE = "You are reviewing merge request 42 in the repository `example/service`. Report how many files it changes and name them."


class Result:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []
        self.failed = False

    def add(self, status: str, name: str, detail: str) -> None:
        self.rows.append((status, name, detail))
        if status == FAIL:
            self.failed = True

    def render(self) -> None:
        print()
        for status, name, detail in self.rows:
            print(f"  [{status}] {name:<26} {detail}")
        print()
        if self.failed:
            print("  VERDICT: do not put this model on a tier.")
        else:
            print("  VERDICT: safe to try on a low-blast-radius tier (classifier, then model_easy).")


def check_priceable(model: str, result: Result) -> None:
    """Same gate the engine applies, run before you spend anything.

    completion_cost() returns 0.0 for a model LiteLLM cannot price, so an
    unpriced model does not merely report wrong -- it reports FREE, and every
    spend ceiling built on that number silently stops existing.
    """
    from minions.classifier import is_priceable

    if is_priceable(model):
        result.add(PASS, "priced by litellm", "cost data present; spend ceilings will apply")
        return

    bare = model.split("/")[-1]
    result.add(
        FAIL,
        "priced by litellm",
        f"no cost data for {model!r} (nor bare {bare!r}) -- ceilings would be inert. "
        "Prefer a natively priced prefix, or register a price you can defend.",
    )


def check_tool_call(message, tools: list[dict], result: Result) -> None:
    """The one that matters: a tool call, not prose about a tool call."""
    calls = getattr(message, "tool_calls", None)
    if not calls:
        text = (getattr(message, "content", "") or "").strip().replace("\n", " ")
        result.add(
            FAIL,
            "emitted a tool call",
            f"no tool_calls -- the loop would exit here and record this as the result: {text[:90]!r}",
        )
        return

    known = {t["function"]["name"] for t in tools}
    call = calls[0]
    name = call.function.name

    if name not in known:
        result.add(FAIL, "emitted a tool call", f"invented a tool not in the schema: {name!r}")
        return

    try:
        args = json.loads(call.function.arguments or "{}")
    except json.JSONDecodeError as e:
        result.add(FAIL, "emitted a tool call", f"{name}(...) arguments are not valid JSON: {e}")
        return

    result.add(PASS, "emitted a tool call", f"{name}({', '.join(args) or ''})")
    _check_required_args(name, args, tools, result)


def _check_required_args(name: str, args: dict, tools: list[dict], result: Result) -> None:
    """Catches the loud cousin of the silent failure.

    A missing required argument raises inside the executor and gets retried, so
    it costs turns rather than correctness -- but a model that does it often
    will burn the turn ceiling before it finishes anything.
    """
    schema = next(t for t in tools if t["function"]["name"] == name)
    required = schema["function"].get("parameters", {}).get("required", [])
    missing = [r for r in required if r not in args]

    if missing:
        result.add(WARN, "required args present", f"{name} omitted {missing} -- executor would raise and retry")
    else:
        result.add(PASS, "required args present", f"all {len(required)} required arg(s) supplied")


def check_cost(response, model: str, result: Result) -> None:
    """A price map entry is necessary but not sufficient -- confirm the real call prices."""
    import litellm

    try:
        cost = litellm.completion_cost(completion_response=response)
    except Exception as e:
        result.add(FAIL, "cost resolves", f"completion_cost() raised: {type(e).__name__}: {e}")
        return

    if cost and cost > 0:
        result.add(PASS, "cost resolves", f"${cost:.6f} for this call")
        return

    result.add(
        FAIL,
        "cost resolves",
        "completion_cost() returned 0 -- agents.cost_usd, --costs and the dashboard would read $0",
    )


def check_reasoning(message, result: Result) -> None:
    """NanoGPT's docs warn LiteLLM may fail to parse reasoning content, and
    Kimi K2.7-Code has forced thinking. Informational: reasoning is not a
    problem, silently losing it or choking on it is."""
    reasoning = getattr(message, "reasoning_content", None)

    if reasoning:
        result.add(WARN, "reasoning content", f"present and parsed ({len(reasoning)} chars) -- watch output-token spend")
    else:
        result.add(PASS, "reasoning content", "none returned, or cleanly absent")


def check_cache_control(model: str, result: Result) -> None:
    """Informational, not a gate: caching is an economics question.

    Ask the predicate directly rather than diffing apply_cache_control's output.
    That function also returns the list unchanged when there is no system
    message and fewer than three entries, so a synthetic probe reads as
    "unsupported" on Anthropic too -- a false negative that would make every
    model look equally uncached.
    """
    from minions.agents.caching import supports_caching

    if supports_caching(model):
        result.add(PASS, "prompt caching", "cache_control supported; ~44% of input served from cache today")
        return

    result.add(
        WARN,
        "prompt caching",
        "unsupported -- input bills at list price, not the ~10% cache-read rate. Compare against Anthropic's EFFECTIVE cost, not its list price.",
    )


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("model", help="LiteLLM model string, e.g. moonshot/kimi-k2.7-code or nano-gpt/<id>")
    parser.add_argument("--role", default="code_reviewer", help="whose tool schema to probe with (default: code_reviewer)")
    args = parser.parse_args()

    import litellm

    from minions.agents.tools.definitions import get_tools_for_role

    tools = get_tools_for_role(args.role)
    result = Result()

    print(f"\n  model: {args.model}")
    print(f"  role : {args.role}  ({len(tools)} tools in schema)")

    check_priceable(args.model, result)
    check_cache_control(args.model, result)

    try:
        # Mirrors runner.py:508 -- same shape, no tool_choice.
        response = await litellm.acompletion(
            model=args.model,
            messages=[{"role": "user", "content": PROBE}],
            tools=tools,
            max_tokens=8192,
            timeout=120,
        )
    except Exception as e:
        result.add(FAIL, "call completes", f"{type(e).__name__}: {str(e)[:160]}")
        result.render()
        return 1

    result.add(PASS, "call completes", f"finish_reason={response.choices[0].finish_reason}")

    message = response.choices[0].message
    check_tool_call(message, tools, result)
    check_reasoning(message, result)
    check_cost(response, args.model, result)

    result.render()
    if result.failed:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
