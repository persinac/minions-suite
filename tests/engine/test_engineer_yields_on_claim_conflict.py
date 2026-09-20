"""`run_engineer` must stand down when the database says it lost the claim.

The in-process launcher and an external herder can both decide the same task is
unowned. The reachable case is the unclaimed-item fallback (`dev.py`'s
`unclaimed` sweep), which reads the agent row of the CURRENT attempt: a herder
that claims in the same moment leaves both paths believing they should run.
`idx_agents_one_live_per_task` now refuses the second insert, and the question
these tests answer is what happens to the loser.

It must be caught, not raised. `_spawn` is a bare `asyncio.create_task` with no
exception isolation, so an uncaught `AgentClaimConflictError` here is a silent
task death with nothing recorded against the job — strictly worse than the
duplicate it was meant to prevent, because at least the duplicate is visible.

Asserted structurally rather than by driving the engine. `run_engineer` needs a
job, a task, a project, a git provider and a model before it reaches the insert,
and a test that mocked all five would be asserting on the mocks. The structure
is the whole claim: the catch has to WRAP the create_agent call and it has to
end in a return. `inspect.getsource` substring matching — the style of
`test_retry_launches_agent.py` — cannot tell a handler that wraps the call from
one that sits elsewhere in the same function, which is exactly the distinction
that matters here. So this walks the AST instead.
"""

import ast
import inspect

from minions.engine.dev import run_engineer

CONFLICT = "AgentClaimConflictError"


def _tree() -> ast.Module:
    return ast.parse(inspect.cleandoc(inspect.getsource(run_engineer)))


def _calls(node: ast.AST) -> list[str]:
    """Dotted names of every call in a subtree, e.g. `engine.db.create_agent`."""
    names = []
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        names.append(ast.unparse(child.func))
    return names


def _handler_names(handler: ast.ExceptHandler) -> list[str]:
    if handler.type is None:
        return []
    if isinstance(handler.type, ast.Tuple):
        return [ast.unparse(e) for e in handler.type.elts]
    return [ast.unparse(handler.type)]


def _guarding_try() -> ast.Try:
    """The `try` whose body creates the agent row.

    Finding it by its body rather than by its handler is deliberate: a test that
    searched for the handler first would pass even if someone moved the
    `create_agent` call out from under it.
    """
    guards = [
        n
        for n in ast.walk(_tree())
        if isinstance(n, ast.Try) and any(c.endswith("create_agent") for c in _calls(ast.Module(body=n.body, type_ignores=[])))
    ]
    assert len(guards) == 1, f"expected exactly one try wrapping create_agent, found {len(guards)}"
    return guards[0]


class TestTheConflictIsCaught:
    def test_the_create_agent_call_is_inside_a_try(self):
        """The property `inspect.getsource` substring matching cannot check."""
        body_calls = _calls(ast.Module(body=_guarding_try().body, type_ignores=[]))

        assert any(c.endswith("create_agent") for c in body_calls)

    def test_a_handler_names_the_claim_conflict(self):
        """By name, not a bare `except Exception` that happens to cover it.

        A broad handler would swallow a genuine launch failure as "someone else
        got there first" and record the wrong event against the job.
        """
        named = [n for h in _guarding_try().handlers for n in _handler_names(h)]

        assert CONFLICT in named, f"handlers catch {named}, not {CONFLICT}"

    def test_the_handler_is_not_a_bare_except(self):
        """`except:` would also catch KeyboardInterrupt and SystemExit."""
        assert all(h.type is not None for h in _guarding_try().handlers)


class TestTheLoserStandsDown:
    def _conflict_handler(self) -> ast.ExceptHandler:
        for handler in _guarding_try().handlers:
            if CONFLICT in _handler_names(handler):
                return handler
        raise AssertionError(f"no handler for {CONFLICT}")

    def test_it_returns_rather_than_continuing(self):
        """Falling through would run the engineer with the WINNER's agent row.

        The code below the insert records `agent_launched`, publishes NATS
        status and comments on Trello using `agent.id`. Without the return, the
        loser would narrate the winner's work and then run a second engineer
        against the same task anyway — the duplicate PR this change exists to
        prevent.
        """
        handler = self._conflict_handler()

        assert isinstance(handler.body[-1], ast.Return), f"handler ends in {type(handler.body[-1]).__name__}, not Return"
        assert handler.body[-1].value is None

    def test_it_records_the_skip_against_the_job(self):
        """A silent stand-down is indistinguishable from a crash in `_spawn`.

        `record_event` is the only durable trace: the log line goes to the pod's
        stdout and is gone at the next rollout, while the job's event stream is
        what anyone reconstructs a mystery from afterwards.
        """
        handler = self._conflict_handler()

        assert any(c.endswith("record_event") for c in _calls(ast.Module(body=handler.body, type_ignores=[])))

    def test_it_does_not_swallow_the_conflict_without_a_trace(self):
        """A handler whose body is just `return` would pass every test above."""
        handler = self._conflict_handler()
        source = ast.unparse(ast.Module(body=handler.body, type_ignores=[]))

        assert "agent_launch_skipped" in source, f"the skip event lost its name: {source}"


class TestTheImportIsReal:
    def test_the_exception_is_imported_from_the_db_layer(self):
        """Not redefined locally, and not caught by a same-named string.

        `except AgentClaimConflictError` against a name that resolves to
        something else would never fire, and the test above would still be
        green — it reads the source, not the binding.
        """
        import minions.engine.dev as dev_mod
        from minions.db import AgentClaimConflictError

        assert dev_mod.AgentClaimConflictError is AgentClaimConflictError
