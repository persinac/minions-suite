"""complete_subtask must accept success, not police the ceremony around it.

The machine requires pending -> running -> completed. An agent that did the
work but skipped start_subtask used to get "Invalid subtask transition:
pending -> completed" — an error naming no remedy. Job 3b8b8ba9's attempt 1
finished all its code, hit exactly that on its final subtask, gave up, and
"agent finished with incomplete subtasks" consumed the attempt. The tool now
walks the legal path on the agent's behalf, and re-completing finished work
is idempotent agreement rather than an error.
"""

import json

from fastmcp import Client

from minions.core.models import AgentRole, JobStatus, Subtask, SubtaskStatus, Task
from minions.server.mcp import create_server


async def _subtask_in(db, status: SubtaskStatus):
    job = await db.create_job("spec")
    for s in (JobStatus.SPEC_READY, JobStatus.TASKS_CREATED, JobStatus.DEV_IN_PROGRESS):
        await db.update_job_status(job.id, s)
    task = await db.create_task(Task(job_id=job.id, title="t", description="d", service="api", agent_role=AgentRole.BACKEND_ENGINEER))
    subtask = await db.create_subtask(Subtask(task_id=task.id, sequence_num=1, description="do the thing"))
    if status != SubtaskStatus.PENDING:
        await db.update_subtask(subtask.id, status=SubtaskStatus.RUNNING)
    if status == SubtaskStatus.COMPLETED:
        await db.update_subtask(subtask.id, status=SubtaskStatus.COMPLETED)
    return subtask


async def _complete(db, subtask_id: str, result: str | None = None):
    server = create_server(db)
    async with Client(server) as client:
        args = {"subtask_id": subtask_id}
        if result is not None:
            args["result"] = result
        response = await client.call_tool("complete_subtask", args)
    return json.loads(response.content[0].text)


class TestCompletionIsForgiving:
    async def test_a_pending_subtask_is_walked_through_running_to_completed(self, db):
        subtask = await _subtask_in(db, SubtaskStatus.PENDING)

        out = await _complete(db, subtask.id, result="done it")

        assert out == {"subtask_id": subtask.id, "status": "completed"}
        refreshed = await db.get_subtask(subtask.id)
        assert refreshed.status == SubtaskStatus.COMPLETED
        assert refreshed.started_at, "the auto-start must leave a real started_at, not a hole in the ledger"
        assert refreshed.result == {"output": "done it"}

    async def test_a_running_subtask_completes_as_before(self, db):
        subtask = await _subtask_in(db, SubtaskStatus.RUNNING)

        out = await _complete(db, subtask.id)

        assert out["status"] == "completed"
        assert (await db.get_subtask(subtask.id)).status == SubtaskStatus.COMPLETED

    async def test_recompleting_a_completed_subtask_is_agreement_not_an_error(self, db):
        subtask = await _subtask_in(db, SubtaskStatus.COMPLETED)

        out = await _complete(db, subtask.id)

        assert out == {"subtask_id": subtask.id, "status": "completed"}

    async def test_a_missing_subtask_is_still_an_error(self, db):
        out = await _complete(db, "no-such-id")

        assert "not found" in out["error"]
