"""A status name that cannot possibly be valid should not cost a round trip.

On job 7b840e7f an engineer called update_task_status with `completed`, then
`done`, then `pr_created`. Only the middle one is even a TaskStatus. The other
two travelled to the Arbiter, were refused there, and (before the breaker was
rescoped) counted as system failures that opened the circuit for 300s.

Two things are wrong with letting them travel. The Arbiter is the wrong place to
learn that a string is not a member of an enum — that is knowable locally,
instantly, without NATS. And the refusal that comes back names no alternative,
so the agent guesses again, which is precisely what it did.
"""

import json

import pytest
from fastmcp import Client

from minions.config import Config
from minions.core.models import AgentRole, JobStatus, Task, TaskStatus
from minions.server.mcp import create_server


@pytest.fixture
async def mcp_client(db):
    server = create_server(db, Config.from_env())
    async with Client(server) as client:
        yield client


async def _call(client, tool: str, args: dict) -> dict:
    result = await client.call_tool(tool, args)
    return json.loads(result.content[0].text)


async def _task(db) -> str:
    job = await db.create_job("spec")
    for status in (JobStatus.SPEC_READY, JobStatus.TASKS_CREATED, JobStatus.DEV_IN_PROGRESS):
        await db.update_job_status(job.id, status)
    task = await db.create_task(Task(job_id=job.id, title="t", description="d", service="svc", agent_role=AgentRole.BACKEND_ENGINEER))
    await db.update_task(task.id, status=TaskStatus.IN_PROGRESS)
    return task.id


class TestUnknownStatusNames:
    async def test_a_subtask_status_is_refused_with_a_pointer(self, mcp_client, db):
        """`completed` is a real status — on subtasks. That is why it gets
        reached for, and why the error has to name the distinction."""
        task_id = await _task(db)

        payload = await _call(mcp_client, "update_task_status", {"task_id": task_id, "status": "completed"})

        assert "error" in payload
        assert "SUBTASK" in payload["error"] or "subtask" in payload["error"]
        assert "valid_statuses" in payload

    async def test_an_invented_status_lists_the_real_ones(self, mcp_client, db):
        """`pr_created` is nobody's status. The reply must end the guessing."""
        task_id = await _task(db)

        payload = await _call(mcp_client, "update_task_status", {"task_id": task_id, "status": "pr_created"})

        assert "error" in payload
        assert "pr_open" in payload["valid_statuses"]

    async def test_a_pr_shaped_guess_is_pointed_at_report_pr(self, mcp_client, db):
        """update_task_status cannot record a url or number, so an agent that
        reaches for it about a PR is already on the wrong path."""
        task_id = await _task(db)

        payload = await _call(mcp_client, "update_task_status", {"task_id": task_id, "status": "pr_created"})

        assert "report_pr" in payload["error"]

    async def test_every_valid_status_is_offered(self, mcp_client, db):
        task_id = await _task(db)

        payload = await _call(mcp_client, "update_task_status", {"task_id": task_id, "status": "nonsense"})

        assert set(payload["valid_statuses"]) == {s.value for s in TaskStatus}

    async def test_a_valid_status_still_works(self, mcp_client, db):
        """The guard must not become a wall — legal calls pass through."""
        task_id = await _task(db)

        payload = await _call(mcp_client, "update_task_status", {"task_id": task_id, "status": "failed"})

        assert "error" not in payload
        assert payload["status"] == "failed"

    async def test_an_unknown_status_never_reaches_the_arbiter(self, mcp_client, db, monkeypatch):
        """The point of validating locally. If this call proposes a transition,
        a refusal round trip is being spent on a string that was never a
        TaskStatus.

        A NATS client has to be installed for this to mean anything: with
        `_nats_client` at its test default of None the tool takes the direct-DB
        branch and could never have proposed a transition, so the assertion
        would hold for the broken code too.
        """
        import minions.server.mcp as mcp_mod

        proposed = []

        async def _spy(*args, **kwargs):
            proposed.append(args)
            return {"approved": True}

        monkeypatch.setattr(mcp_mod, "_nats_client", object())
        monkeypatch.setattr(mcp_mod, "_propose_transition", _spy)
        task_id = await _task(db)

        await _call(mcp_client, "update_task_status", {"task_id": task_id, "status": "pr_created"})

        assert proposed == [], "a string that is not a TaskStatus must be refused locally, not round-tripped to the Arbiter"

        # Control: a legal status DOES go to the Arbiter, so the assertion above
        # is about the validation and not about the NATS branch being dead.
        await _call(mcp_client, "update_task_status", {"task_id": task_id, "status": "failed"})
        assert len(proposed) == 1
