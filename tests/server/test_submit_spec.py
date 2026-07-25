"""submit_spec — the MCP intake path.

This tool had no test and had never worked. It built a Job and passed it to
db.create_job(), whose signature is (spec: str, external_id) — create_job
constructs the Job itself. Every call failed pydantic validation with
"Input should be a valid string", which surfaced only as a ToolError to whoever
was submitting.

Exercised through an in-memory FastMCP client so the tool is invoked the way a
real caller invokes it — through the MCP layer, with the same argument
marshalling — rather than by reaching past it to the closure.
"""

import json

import pytest
from fastmcp import Client

from minions.config import Config
from minions.core.models import JobStatus
from minions.server.mcp import create_server


@pytest.fixture
async def mcp_client(db):
    """In-memory MCP client wired to a server backed by the real test database."""
    server = create_server(db, Config.from_env())
    async with Client(server) as client:
        yield client


async def _call(client, tool: str, args: dict) -> dict:
    result = await client.call_tool(tool, args)
    return json.loads(result.content[0].text)


class TestSubmitSpec:
    async def test_creates_a_job_and_returns_its_id(self, mcp_client, db):
        payload = await _call(mcp_client, "submit_spec", {"spec": "Add a healthcheck endpoint"})

        assert payload["job_id"]
        assert payload["status"] == str(JobStatus.SPEC_RECEIVED)

        # The job must actually be in the database — a tool that returns a
        # plausible id without persisting would pass a shallower assertion.
        job = await db.get_job(payload["job_id"])
        assert job is not None
        assert job.spec == "Add a healthcheck endpoint"
        assert job.status == JobStatus.SPEC_RECEIVED

    async def test_persists_external_id_for_trello_linkage(self, mcp_client, db):
        """external_id is how a job is traced back to its Trello card."""
        payload = await _call(
            mcp_client,
            "submit_spec",
            {"spec": "Backfill stats", "external_id": "trello:1M8TflPY"},
        )

        job = await db.get_job(payload["job_id"])
        assert job.external_id == "trello:1M8TflPY"

    async def test_external_id_is_optional(self, mcp_client, db):
        payload = await _call(mcp_client, "submit_spec", {"spec": "No card attached"})
        job = await db.get_job(payload["job_id"])
        assert job.external_id in (None, "")

    async def test_multiline_spec_survives_intact(self, mcp_client, db):
        """Real specs are markdown documents, not one-liners."""
        spec = "# Title\n\n## Context\nSome detail.\n\n- bullet\n- bullet\n"
        payload = await _call(mcp_client, "submit_spec", {"spec": spec})

        job = await db.get_job(payload["job_id"])
        assert job.spec == spec

    async def test_the_tool_is_actually_exposed(self, mcp_client):
        """Guards against the tool being dropped from the server registration."""
        names = [t.name for t in await mcp_client.list_tools()]
        assert "submit_spec" in names
