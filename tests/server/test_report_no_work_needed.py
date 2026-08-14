"""The tool an engineer calls when the work is already done.

Its guards matter more than its happy path. This closes a task terminally with
no PR, so an unexplained or careless call silently ends work that may genuinely
have been needed.
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


async def _task(db) -> tuple[str, str]:
    job = await db.create_job("spec")
    for status in (JobStatus.SPEC_READY, JobStatus.TASKS_CREATED, JobStatus.DEV_IN_PROGRESS):
        await db.update_job_status(job.id, status)
    task = await db.create_task(Task(job_id=job.id, title="t", description="d", service="svc", agent_role=AgentRole.BACKEND_ENGINEER))
    await db.update_task(task.id, status=TaskStatus.IN_PROGRESS)
    return job.id, task.id


class TestReportNoWorkNeeded:
    async def test_it_closes_the_task_without_a_pr(self, mcp_client, db):
        _, task_id = await _task(db)

        payload = await _call(
            mcp_client,
            "report_no_work_needed",
            {"task_id": task_id, "reason": "already added in commit 8997ef8 — tests/test_play_transaction_crud_db.py"},
        )

        assert "error" not in payload, payload
        assert (await db.get_task(task_id)).status == TaskStatus.NO_WORK_NEEDED

    async def test_an_unexplained_claim_is_refused(self, mcp_client, db):
        """This ends the task terminally. "Nothing to do" with no evidence is
        indistinguishable from an agent giving up."""
        _, task_id = await _task(db)

        payload = await _call(mcp_client, "report_no_work_needed", {"task_id": task_id, "reason": "   "})

        assert "error" in payload
        assert (await db.get_task(task_id)).status == TaskStatus.IN_PROGRESS

    async def test_a_task_with_a_pr_is_refused(self, mcp_client, db):
        """A task that opened a PR plainly needed doing. Accepting this would
        strand the PR the way job 7b840e7f's was stranded."""
        _, task_id = await _task(db)
        await db.update_task(task_id, pr_url="https://github.com/o/r/pull/29", pr_number=29)

        payload = await _call(mcp_client, "report_no_work_needed", {"task_id": task_id, "reason": "changed my mind"})

        assert "error" in payload
        assert "already has a PR" in payload["error"]
        assert (await db.get_task(task_id)).status == TaskStatus.IN_PROGRESS

    async def test_the_reason_is_recorded_for_a_human_to_check(self, mcp_client, db):
        """The claim is self-reported, so the evidence has to be durable."""
        job_id, task_id = await _task(db)

        await _call(mcp_client, "report_no_work_needed", {"task_id": task_id, "reason": "commit 8997ef8 already did this"})

        events = await db.get_events(job_id)
        matching = [e for e in events if e.get("event_type") == "no_work_needed"]
        assert matching, "the claim must be recorded as an event"
        assert "8997ef8" in str(matching[0])

    async def test_an_unknown_task_errors_cleanly(self, mcp_client):
        payload = await _call(mcp_client, "report_no_work_needed", {"task_id": "nope", "reason": "x"})

        assert "error" in payload
