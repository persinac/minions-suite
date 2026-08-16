"""End-to-end fixtures: a real job, driven by a scripted model.

Everything below the model is real. The engine is a real `JobEngine`, the tools
resolve against a real MCP server, the writes land in the real Postgres test
schema, and every transition goes through the real validators in
`core/state_transitions.py`. The single thing replaced is `litellm.acompletion`
-- the one call in the whole system that reaches the network
(`minions/agents/runner.py:443`).

That seam is what makes these tests worth having. The existing engine tests build
a `JobEngine` with `__new__` and mock its database, which proves a function does
what it says in isolation; it cannot prove that a job started at `spec_received`
arrives at `done`, because no mock is ever refused by a transition map. Here the
state machine gets a vote.

Scripts are keyed by agent role and replayed per agent, so two engineers on one
job each run the role's script from its first turn. Roles reach the fake through
the `metadata.trace_name` the runner already sends for tracing, so nothing had to
be added to production code to make this observable.
"""

import json
from dataclasses import dataclass

import pytest
from litellm import ModelResponse
from litellm.types.utils import ChatCompletionMessageToolCall, Choices, Function, Message, Usage

from minions.config import Config
from minions.engine.job_engine import JobEngine
from minions.server.mcp import create_server


@dataclass(frozen=True)
class Call:
    """One tool call the scripted model will emit."""

    name: str
    args: dict


def turn(*calls: Call) -> list[Call]:
    """One assistant turn, emitting zero or more tool calls."""
    return list(calls)


class ScriptedLLM:
    """A stand-in for `litellm.acompletion` that replays canned tool calls.

    Responses are built from litellm's own `ModelResponse`/`Message` types rather
    than hand-rolled stubs, so everything the runner does to a real response --
    `model_dump(exclude_none=True)`, `extract_cache_tokens`, usage accounting --
    exercises the same code path it does in production. A hand-rolled mock would
    drift from the shape it imitates precisely when litellm changes, which is the
    moment a test like this most needs to be believed.
    """

    def __init__(self):
        self._script: dict[str, list[list[Call]]] = {}
        self._cursor: dict[str, int] = {}
        self.calls: list[tuple[str, str, dict]] = []
        self.prompts: list[tuple[str, str]] = []
        self.tool_names: dict[str, set[str]] = {}

    def script(self, role: str, *turns: list[Call]) -> None:
        """Define the turns `role` will take. A final no-tool turn is implicit."""
        self._script[role] = [list(t) for t in turns]

    def calls_for(self, role: str) -> list[tuple[str, dict]]:
        return [(name, args) for r, name, args in self.calls if r == role]

    def called(self, role: str, tool: str) -> bool:
        return any(name == tool for name, _ in self.calls_for(role))

    async def acompletion(self, **kwargs):
        metadata = kwargs.get("metadata") or {}
        role = metadata.get("trace_name") or "agent"
        agent_id = metadata.get("trace_user_id") or role

        messages = kwargs.get("messages") or []
        if messages:
            system = messages[0].get("content")
            if isinstance(system, list):  # cache_control blocks
                system = " ".join(str(b.get("text", "")) for b in system)
            self.prompts.append((role, str(system)))

        self.tool_names[role] = {t["function"]["name"] for t in (kwargs.get("tools") or [])}

        turns = self._script.get(role, [])
        index = self._cursor.get(agent_id, 0)
        self._cursor[agent_id] = index + 1

        if index >= len(turns):
            return _response(role, [], finish="stop")

        planned = turns[index]
        for call in planned:
            self.calls.append((role, call.name, call.args))
        return _response(role, planned, finish="tool_calls" if planned else "stop")


def _response(role: str, calls: list[Call], finish: str) -> ModelResponse:
    tool_calls = [
        ChatCompletionMessageToolCall(
            id=f"call_{role}_{i}",
            type="function",
            function=Function(name=c.name, arguments=json.dumps(c.args)),
        )
        for i, c in enumerate(calls)
    ]
    message = Message(
        content=None if tool_calls else f"{role} finished.",
        tool_calls=tool_calls or None,
        role="assistant",
    )
    return ModelResponse(
        choices=[Choices(finish_reason=finish, index=0, message=message)],
        usage=Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        model="scripted/test-model",
    )


@pytest.fixture
def scripted_llm(monkeypatch) -> ScriptedLLM:
    """Replace the one networked call in the system with a replayable script."""
    fake = ScriptedLLM()
    monkeypatch.setattr("litellm.acompletion", fake.acompletion)
    # completion_cost has no prices for a fake model. The runner already guards
    # the call, but letting it raise on every turn would bury real failures in
    # traceback noise.
    monkeypatch.setattr("litellm.completion_cost", lambda **_: 0.0)
    return fake


@pytest.fixture
def e2e_config(tmp_path) -> Config:
    """A Config with every outbound integration off.

    `arbiter_enabled=False` selects the in-engine fallback in `launch_arbiter`,
    which advances the job once real tasks exist. The NATS arbiter path is
    covered separately -- it is a different mechanism, not a different setting.
    """
    # A real project with a real service. run_engineer refuses a task whose
    # service it cannot resolve ("Unknown service ..."), so an empty registry
    # would make every engineer test pass for the wrong reason -- no agent ever
    # starts, and an assertion about what the agent did finds nothing to contradict
    # it. repo_path points at a real directory but no clone_url, which keeps
    # ensure_checkout out of the picture.
    repo = tmp_path / "repo"
    repo.mkdir()
    projects = tmp_path / "projects.yaml"
    projects.write_text(
        "projects:\n"
        "  testproj:\n"
        "    project_id: testproj\n"
        "    git_provider: github\n"
        f"    repo_path: {repo}\n"
        "    services:\n"
        "      api:\n"
        "        language: python\n"
        f"        repo_path: {repo}\n"
    )
    return Config(
        model="scripted/test-model",
        projects_file=str(projects),
        arbiter_enabled=False,
        classifier_enabled=False,
        memory_enabled=False,
        job_cost_limit_usd=0.0,
        agent_cost_limit_usd=0.0,
        dry_run=False,
    )


@pytest.fixture
async def e2e_engine(db, e2e_config) -> JobEngine:
    """A real JobEngine whose tools resolve against a real MCP server."""
    server = create_server(db, e2e_config)
    return JobEngine(db=db, config=e2e_config, mcp_server=server)
