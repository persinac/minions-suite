"""create_task's routing guards: membership, then grounding.

Job 9a1aeba4 is the receipt. The arbiter routed a flashback-cns code task (the
spec named the repo repeatedly and cited services/game_play_router) to
flashback-process — a docs repo registered ten minutes earlier. create_task's
docstring had always CLAIMED the service must come from the services
configuration; nothing enforced it, and a valid-but-wrong name passed every
later check because _resolve_service only asks whether the name exists.

The grounding refusal is refuse-ONCE per (job, service): the identical retry is
accepted, so a deliberate arbiter is delayed one turn and a wedge is
impossible. That acceptance must survive the event write failing, because
record_event swallows all failures in both backends — hence the in-memory
warned-set and the no-op-record test at the bottom.
"""

import json
import textwrap

import pytest
from fastmcp import Client

from minions.config import Config
from minions.core.models import TaskStatus
from minions.server import mcp as mcp_module
from minions.server.mcp import create_server

TWO_SERVICE_YAML = textwrap.dedent(
    """
    projects:
      flashback-cns:
        project_id: flippin-balls/flashback-cns
        description: "CNS: MQTT/LoRa command routing services for the pinball fleet"
        services:
          flashback-cns:
            project_id: flippin-balls/flashback-cns
            clone_url: https://github.com/flippin-balls/flashback-cns.git
            repo_path: /repos/flashback-cns
            language: python
      flashback-process:
        project_id: flippin-balls/flashback-process
        description: "Cross-repo process, runbook, and design docs"
        services:
          flashback-process:
            project_id: flippin-balls/flashback-process
            clone_url: https://github.com/flippin-balls/flashback-process.git
            repo_path: /repos/flashback-process
    """
)


@pytest.fixture(autouse=True)
def _clean_warned_state():
    """The refuse-once record is module state and must not leak across tests."""
    mcp_module._service_mismatch_warned.clear()
    yield
    mcp_module._service_mismatch_warned.clear()


@pytest.fixture
async def mcp_client(db, tmp_path):
    projects_file = tmp_path / "projects.yaml"
    projects_file.write_text(TWO_SERVICE_YAML)
    config = Config.from_env()
    config.projects_file = str(projects_file)
    server = create_server(db, config)
    async with Client(server) as client:
        yield client


async def _call(client, args: dict) -> dict:
    result = await client.call_tool("create_task", args)
    return json.loads(result.content[0].text)


def _args(job_id: str, service: str) -> dict:
    return {
        "job_id": job_id,
        "title": "Unblock the router event loop",
        "description": "pipeline the batch fetch",
        "service": service,
        "agent_role": "backend_engineer",
    }


THE_SPEC = "The latency is in services/game_play_router. Investigated in flashback-cns. Keep S.1 signing."


class TestMembership:
    async def test_an_unregistered_name_is_rejected_with_the_valid_list(self, mcp_client, db):
        job = await db.create_job(THE_SPEC)

        payload = await _call(mcp_client, _args(job.id, "store-game-router"))

        assert "error" in payload
        assert payload.get("retryable") is True
        assert "flashback-cns" in payload["error"], "the error must hand back the valid names, not just say no"
        assert await db.get_tasks(job.id) == []

    async def test_reserved_names_are_still_rejected_first(self, mcp_client, db):
        """The hermetic e2e suite depends on `_spec` dying here, not in the registry check."""
        job = await db.create_job(THE_SPEC)

        payload = await _call(mcp_client, _args(job.id, "_spec"))

        assert "reserved" in payload["error"]


class TestGrounding:
    async def test_the_misroute_is_refused_once_with_the_evidence(self, mcp_client, db):
        job = await db.create_job(THE_SPEC)

        payload = await _call(mcp_client, _args(job.id, "flashback-process"))

        assert payload.get("retryable") is True
        assert "flashback-cns" in payload["error"]
        assert "Cross-repo process" in payload["error"], "the description is the WHY of the refusal"
        assert await db.get_tasks(job.id) == []
        events = [e for e in await db.get_events(job.id) if e["event_type"] == "service_mismatch"]
        assert len(events) == 1
        assert "service=flashback-process" in events[0]["detail"]

    async def test_the_identical_retry_is_accepted_and_recorded(self, mcp_client, db):
        """Refuse-once: the second call is a deliberate re-derivation and must land."""
        job = await db.create_job(THE_SPEC)
        await _call(mcp_client, _args(job.id, "flashback-process"))

        payload = await _call(mcp_client, _args(job.id, "flashback-process"))

        assert "task_id" in payload, payload
        tasks = await db.get_tasks(job.id)
        assert len(tasks) == 1
        assert tasks[0].service == "flashback-process"
        events = [e for e in await db.get_events(job.id) if e["event_type"] == "service_mismatch"]
        assert any("accepted_on_retry=true" in e["detail"] for e in events)

    async def test_the_grounded_choice_passes_without_ceremony(self, mcp_client, db):
        job = await db.create_job(THE_SPEC)

        payload = await _call(mcp_client, _args(job.id, "flashback-cns"))

        assert "task_id" in payload, payload
        assert (await db.get_tasks(job.id))[0].status == TaskStatus.PENDING

    async def test_a_spec_naming_no_service_grounds_any_choice(self, mcp_client, db):
        """Most tickets describe behaviour, not repos. No evidence, no refusal."""
        job = await db.create_job("make the insert tokens button faster under load")

        payload = await _call(mcp_client, _args(job.id, "flashback-process"))

        assert "task_id" in payload, payload

    async def test_evidence_in_the_original_spec_counts(self, mcp_client, db):
        """Refinement replaces jobs.spec; the raw ticket survives in
        original_spec and may hold the only repo mention."""
        job = await db.create_job("fix the router lag — code is in flashback-cns")
        await db.update_job_spec(job.id, "Refined: unblock the event loop. ## Assumptions\nNone")

        payload = await _call(mcp_client, _args(job.id, "flashback-cns"))

        assert "task_id" in payload, payload

    async def test_a_different_ungrounded_service_is_refused_separately(self, mcp_client, db):
        """The refuse-once key is (job, service) — a new wrong answer gets its own warning."""
        spec = "wallet work, see flippin-balls/flashback-cns"
        job = await db.create_job(spec)
        await _call(mcp_client, _args(job.id, "flashback-process"))  # warned

        payload = await _call(mcp_client, _args(job.id, "flashback-process"))
        assert "task_id" in payload, "the warned pair must be accepted"

    async def test_acceptance_survives_a_silent_event_store(self, mcp_client, db, monkeypatch):
        """record_event swallows failures in both backends. If the warning event
        was never written AND the in-memory set were the only record to fail
        too, the guard would refuse the identical retry forever — the one wedge
        this design promises cannot happen."""

        async def _swallowed(*args, **kwargs):
            return None

        monkeypatch.setattr(db, "record_event", _swallowed)
        job = await db.create_job(THE_SPEC)

        first = await _call(mcp_client, _args(job.id, "flashback-process"))
        second = await _call(mcp_client, _args(job.id, "flashback-process"))

        assert "error" in first
        assert "task_id" in second, "the in-memory warned-set must carry the refuse-once state alone"


class TestZeroConfig:
    async def test_an_empty_registry_skips_both_guards(self, db, tmp_path):
        """Zero-config setups keep _resolve_service's sole-service fallback."""
        config = Config.from_env()
        config.projects_file = str(tmp_path / "does-not-exist.yaml")
        server = create_server(db, config)
        job = await db.create_job(THE_SPEC)

        async with Client(server) as client:
            payload = await _call(client, _args(job.id, "anything-at-all"))

        assert "task_id" in payload, payload
