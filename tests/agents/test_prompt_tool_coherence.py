"""Every tool a prompt tells an agent to call must exist for that agent.

An unknown tool call is not a no-op. `McpToolExecutor.execute` returns
`{"error": "Unknown tool: ..."}` (minions/agents/tools/mcp_executor.py), which the
model reads as a recoverable failure -- so it rephrases and tries again, spending
turns from a budget the engineer prompt itself describes as the thing that
determines whether work reaches git at all.

Three real instances existed when this test was written, all invisible because
nothing compared the two halves:

  * spec_analyst.md walked the agent through `create_task` / `mark_tasks_created`.
    Those belong to the arbiter; the analyst's tool list is three entries long.
  * arbiter.md said "use `get_messages` to check for incoming messages" while
    ARBITER_TOOL_DEFINITIONS omitted it -- the coordinator could send and never
    receive.
  * engineer.md, shared by three roles, walks through a subtask plan and a pull
    request. `database_engineer` has neither set of tools and no PR cycle in its
    state machine.

The first two are plain contradictions. The third is the harder case a shared
prompt file creates, and it is why this test checks two different things.
"""

import re
from pathlib import Path

import pytest

from minions.agents.prompt import _ROLE_TO_PROMPT
from minions.agents.tools.definitions import get_tools_for_role

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"

# Only instruction-shaped references. A prompt may name another agent's tool to
# tell an agent NOT to reach for it ("you have no `create_pr`"), and flagging that
# would punish precisely the clarity this test exists to encourage.
_INSTRUCTION = re.compile(r"(?:[Uu]se|using|with|call|calls|calling)\s+`([a-z_][a-z0-9_]*)`")


def _referenced_tools(prompt_path: Path) -> set[str]:
    return set(_INSTRUCTION.findall(prompt_path.read_text()))


def orphaned_tools(referenced: set[str], available: set[str]) -> list[str]:
    """Tools a prompt instructs that its readers do not have. Pure, so it is testable."""
    return sorted(referenced - available)


def _tools_for(role: str) -> set[str]:
    return {t["function"]["name"] for t in get_tools_for_role(role)}


def _roles_by_prompt() -> dict[str, list[str]]:
    """Group roles by the prompt file they share (engineer.md serves three)."""
    grouped: dict[str, list[str]] = {}
    for role, rel in _ROLE_TO_PROMPT.items():
        grouped.setdefault(rel, []).append(role)
    return grouped


@pytest.mark.parametrize("rel_path,roles", sorted(_roles_by_prompt().items()))
def test_prompt_never_names_a_tool_no_reader_has(rel_path: str, roles: list[str]):
    """A tool no role reading this file possesses is a plain contradiction.

    Checked against the union of the file's readers, because a shared prompt may
    legitimately instruct `create_pr` on behalf of the roles that can.
    """
    referenced = _referenced_tools(PROMPTS_DIR / rel_path)
    available = set().union(*(_tools_for(r) for r in roles))
    orphaned = orphaned_tools(referenced, available)
    assert not orphaned, (
        f"{rel_path} instructs {orphaned}, which no role reading it has "
        f"(roles: {sorted(roles)}). Either add the tool to that role's definitions "
        f"or stop instructing it -- today the call returns 'Unknown tool' and burns a turn."
    )


@pytest.mark.parametrize("rel_path,roles", sorted(_roles_by_prompt().items()))
def test_shared_prompt_warns_roles_that_lack_a_tool(rel_path: str, roles: list[str]):
    """If a shared prompt instructs a tool some reader lacks, it must say so by name.

    This is the `database_engineer` case. It reads engineer.md, which walks through
    `submit_subtask_plan` and `create_pr` for its two siblings. Deleting those
    instructions would break the roles that need them, and duplicating the file
    invites drift -- so the requirement is that the prompt name the role and tell
    it which steps do not apply.
    """
    referenced = _referenced_tools(PROMPTS_DIR / rel_path)
    text = (PROMPTS_DIR / rel_path).read_text()

    for role in sorted(roles):
        missing = sorted(referenced - _tools_for(role))
        if not missing:
            continue
        assert f"`{role}`" in text or role in text, (
            f"{rel_path} instructs {missing}, which `{role}` does not have, and never "
            f"mentions {role} to say so. A shared prompt must name the role whose "
            f"workflow differs, or that agent will spend turns on tools it lacks."
        )


def test_the_check_fails_on_the_bug_it_was_written_for():
    """A green suite proves nothing unless the check can go red.

    This reconstructs the exact arbiter defect: a prompt instructing
    `get_messages` against a tool list that omits it. If this ever passes, the
    parametrized tests above are decorative.
    """
    arbiter_tools_before_the_fix = {"create_task", "mark_tasks_created", "send_message", "submit_refined_spec", "send_heartbeat"}
    referenced = {"send_message", "get_messages"}
    assert orphaned_tools(referenced, arbiter_tools_before_the_fix) == ["get_messages"]
    # And the real arbiter, post-fix, is clean.
    assert orphaned_tools(referenced, _tools_for("arbiter")) == []


def test_the_detector_actually_detects():
    """Guard the regex itself: a test that silently matches nothing always passes."""
    assert _INSTRUCTION.findall("Use `submit_refined_spec` to store it") == ["submit_refined_spec"]
    assert _INSTRUCTION.findall("Create a PR using `create_pr`") == ["create_pr"]
    # Negative mentions must NOT register, or honest caveats become failures.
    assert _INSTRUCTION.findall("You have no `create_pr`.") == []
    assert _INSTRUCTION.findall("stop reaching for `create_task`") == []


def test_every_role_has_a_prompt_and_tools():
    """A role mapped to no prompt silently falls back to engineer.md (see build_agent_prompt)."""
    for role in _ROLE_TO_PROMPT:
        assert (PROMPTS_DIR / _ROLE_TO_PROMPT[role]).is_file(), f"{role} maps to a missing prompt file"
        assert _tools_for(role), f"{role} has no tools at all"
