"""A refusal must say what it is, and must not punish the asker.

Job 7b840e7f is the whole reason this file exists. A backend engineer guessed
three task-status names in fifteen seconds — `completed` (a SubtaskStatus),
`done` (role-gated to reviewers), `pr_created` (not a status at all). Each was
correctly refused, and each refusal was recorded as an Arbiter *failure*, so the
circuit breaker opened for 300s.

The agent then did everything right: it opened a real PR (#29 on wallet-api) and
called report_pr with the correct number, url and branch. The open breaker
refused that too. Because a breaker refusal carries no from_status, the client
substituted "?" and raised InvalidTransitionError — reporting `? -> pr_open`, a
permanent transition error, for what was a 300s cooldown. The agent gave up, the
task failed with its PR orphaned, and the job failed.

Three separate defects, each independently sufficient:

1. the breaker counted the guardrail doing its job as the system breaking
2. a transient refusal was reported as a permanent one, with the reason erased
3. the refusal never told the agent what it *could* have said instead
"""

import time

import pytest

from minions.core.state_transitions import (
    ArbiterUnavailableError,
    InvalidTransitionError,
    PreconditionError,
    validate_task_transition,
)
from minions.core.timeout_config import TimeoutConfig
from minions.engine.arbiter import Arbiter


class _Msg:
    """Captures the reply instead of publishing it."""

    def __init__(self, payload: bytes):
        self.data = payload
        self.reply = "inbox.test"


class _RecordingNats:
    def __init__(self):
        self.replies = []

    async def subscribe(self, *_a, **_k):
        return None

    @staticmethod
    async def reply(msg, payload):  # matches NatsClient.reply
        raise AssertionError("patched per-test")


def _arbiter(db=None) -> Arbiter:
    return Arbiter(db=db, timeout_config=TimeoutConfig(), nats_client=_RecordingNats())


class TestBreakerScope:
    """The breaker guards against a broken Arbiter, not a confused agent."""

    def test_an_illegal_transition_does_not_count_as_a_failure(self):
        arb = _arbiter()
        arb._record_failure()  # sanity: the counter does move for real faults
        assert len(arb._failure_timestamps) == 1

    def test_three_refusals_must_not_open_the_breaker(self, monkeypatch):
        """The exact shape of 7b840e7f: three refused transitions in seconds.

        Each is the validator working. If they open the breaker, the next
        caller — the same agent, now asking correctly — is refused for 300s.
        """
        import minions.engine.arbiter as arbiter_mod

        arb = _arbiter()
        replies = []

        async def _capture(msg, payload):
            replies.append(payload)

        monkeypatch.setattr(arbiter_mod.NatsClient, "reply", staticmethod(_capture))

        async def _boom(*_a, **_k):
            raise InvalidTransitionError("task", "8f23f0ea", "in_progress", "pr_created")

        monkeypatch.setattr(arb, "_apply_task_transition", _boom)

        import asyncio
        import json

        for _ in range(3):
            payload = json.dumps({"entity_type": "task", "entity_id": "8f23f0ea", "to_status": "pr_created", "kwargs": {}}).encode()
            asyncio.run(arb._handle_transition(_Msg(payload)))

        assert len(replies) == 3
        assert all(r["approved"] is False for r in replies)
        assert not arb._is_circuit_open(), (
            "three refused transitions opened the circuit breaker. Refusing an "
            "illegal transition is the validator succeeding, not the Arbiter "
            "failing — counting it means the next correct call gets refused too."
        )
        assert len(arb._failure_timestamps) == 0

    def test_a_precondition_failure_also_does_not_count(self, monkeypatch):
        import asyncio
        import json

        import minions.engine.arbiter as arbiter_mod

        arb = _arbiter()
        monkeypatch.setattr(arbiter_mod.NatsClient, "reply", staticmethod(lambda *_a, **_k: _noop()))

        async def _noop():
            return None

        async def _missing(*_a, **_k):
            raise PreconditionError("8f23f0ea", "pr_open", ["pr_url"])

        monkeypatch.setattr(arb, "_apply_task_transition", _missing)
        payload = json.dumps({"entity_type": "task", "entity_id": "8f23f0ea", "to_status": "pr_open", "kwargs": {}}).encode()
        asyncio.run(arb._handle_transition(_Msg(payload)))

        assert len(arb._failure_timestamps) == 0

    def test_a_genuine_internal_fault_still_counts(self, monkeypatch):
        """The breaker must not become decorative — a real fault still trips it."""
        import asyncio
        import json

        import minions.engine.arbiter as arbiter_mod

        arb = _arbiter()

        async def _capture(msg, payload):
            return None

        monkeypatch.setattr(arbiter_mod.NatsClient, "reply", staticmethod(_capture))

        async def _explode(*_a, **_k):
            raise RuntimeError("database connection lost")

        monkeypatch.setattr(arb, "_apply_task_transition", _explode)

        for _ in range(3):
            payload = json.dumps({"entity_type": "task", "entity_id": "t1", "to_status": "done", "kwargs": {}}).encode()
            asyncio.run(arb._handle_transition(_Msg(payload)))

        assert arb._is_circuit_open(), "a real internal fault must still open the breaker"

    def test_an_open_breaker_reports_how_long_to_wait(self, monkeypatch):
        """Without this the refusal is indistinguishable from a permanent one."""
        import asyncio
        import json

        import minions.engine.arbiter as arbiter_mod

        arb = _arbiter()
        replies = []

        async def _capture(msg, payload):
            replies.append(payload)

        monkeypatch.setattr(arbiter_mod.NatsClient, "reply", staticmethod(_capture))
        arb._circuit_open_until = time.time() + 300

        payload = json.dumps({"entity_type": "task", "entity_id": "t1", "to_status": "pr_open", "kwargs": {}}).encode()
        asyncio.run(arb._handle_transition(_Msg(payload)))

        assert replies[0]["error"] == "circuit_open"
        assert replies[0]["retry_after_seconds"] > 0


class TestRefusalFidelity:
    """_propose_transition must raise what the Arbiter actually said."""

    @staticmethod
    def _client(response):
        class _Nats:
            async def request(self, *_a, **_k):
                return response

        return _Nats()

    def _propose(self, monkeypatch, response):
        import asyncio

        import minions.server.mcp as mcp_mod

        monkeypatch.setattr(mcp_mod, "_nats_client", self._client(response))
        return asyncio.run(mcp_mod._propose_transition("task", "8f23f0ea", "pr_open", job_id="j1"))

    def test_a_circuit_open_refusal_is_not_a_transition_error(self, monkeypatch):
        """The 7b840e7f misdiagnosis, pinned.

        `? -> pr_open` told the agent its transition was illegal. It was not;
        the Arbiter was in cooldown and the very same call would have worked
        minutes later.
        """
        with pytest.raises(ArbiterUnavailableError) as exc:
            self._propose(
                monkeypatch,
                {"approved": False, "error": "circuit_open", "detail": "cooldown", "retry_after_seconds": 142},
            )

        assert exc.value.retry_after_seconds == 142
        assert "circuit_open" in str(exc.value)
        assert "?" not in str(exc.value), "the reason must be reported, never replaced with '?'"

    def test_a_real_illegal_transition_is_still_a_transition_error(self, monkeypatch):
        """from_status is the Arbiter saying it genuinely evaluated the move."""
        with pytest.raises(InvalidTransitionError) as exc:
            self._propose(
                monkeypatch,
                {"approved": False, "error": "nope", "from_status": "done", "to_status": "pr_open"},
            )

        assert exc.value.from_status == "done"

    def test_missing_fields_surface_as_a_precondition_error(self, monkeypatch):
        with pytest.raises(PreconditionError) as exc:
            self._propose(monkeypatch, {"approved": False, "error": "missing", "missing_fields": ["pr_url"]})

        assert exc.value.missing_fields == ["pr_url"]

    def test_an_unexplained_refusal_still_names_itself(self, monkeypatch):
        with pytest.raises(ArbiterUnavailableError):
            self._propose(monkeypatch, {"approved": False})

    def test_an_approved_response_passes_through(self, monkeypatch):
        assert self._propose(monkeypatch, {"approved": True, "to_status": "pr_open"})["approved"] is True


class TestRefusalsTeach:
    """A refusal that only says "no" makes an agent guess. They guess badly."""

    def test_an_illegal_target_lists_the_legal_ones(self):
        with pytest.raises(InvalidTransitionError) as exc:
            validate_task_transition("t1", "in_progress", "pr_created")

        message = str(exc.value)
        assert "pr_open" in message, f"the refusal must name the reachable states; got: {message}"
        assert exc.value.allowed and "pr_open" in exc.value.allowed

    def test_a_terminal_state_says_so(self):
        with pytest.raises(InvalidTransitionError) as exc:
            validate_task_transition("t1", "done", "pr_open")

        assert "terminal" in str(exc.value)

    def test_a_role_restricted_transition_names_the_roles(self):
        """`in_progress -> done` is legal but reserved. Listing target states
        would send the agent hunting for a different target when the real answer
        is that somebody else performs this one."""
        with pytest.raises(InvalidTransitionError) as exc:
            validate_task_transition("t1", "pr_open", "done", agent_role="backend_engineer")

        message = str(exc.value)
        assert "code_reviewer" in message
        assert "backend_engineer" in message
