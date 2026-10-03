"""submit_scout_finding: the tool boundary that turns a finding into a card.

Scout autoqueue was agreed with no human approval step (2026-10-03), so these
guards are the whole defence against a scout filling the board with vague or
repeated work. Each is driven through a real MCP client against a real DB; only
the Trello call is faked, and it records where the card would have gone.
"""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastmcp import Client

from minions.config import Config
from minions.core.models import AgentRole, Task
from minions.core.stations import SCOUT
from minions.server.mcp import create_server

GOOD = {
    "kind": "hot_spot",
    "title": "routes.py swallows token parse errors and returns 200",
    "evidence": ["src/app/routes.py:142"],
    "scope": "Raise on a bad token in parse_token and map it to 401; leave sessions alone.",
    "oracle": "A new test calling GET /me with token='' gets 401; today it gets 200 with an empty body.",
    "fingerprint": "hot_spot:src/app/routes.py",
}


@pytest.fixture
def filed(monkeypatch):
    """Fake Trello. Records every card, and fails the test if a queue write is attempted."""
    calls: list[dict] = []

    async def fake_file_card(config, list_name, title, description, labels, client=None):
        calls.append({"list": list_name, "title": title, "labels": list(labels), "description": description})
        return {"card_id": f"card-{len(calls)}", "url": f"https://trello.com/c/{len(calls)}"}

    monkeypatch.setattr("minions.providers.trello_cards.file_card", fake_file_card)
    return calls


@pytest.fixture(autouse=True)
def registry(monkeypatch):
    services = {
        "svc-tested": SimpleNamespace(test_command="uv run pytest"),
        "svc-untested": SimpleNamespace(test_command=""),
    }
    monkeypatch.setattr("minions.project_registry.build_registry", lambda path: {"proj": SimpleNamespace(services=services)})


@pytest.fixture
async def client(db):
    config = Config.from_env()
    config.scout_max_findings = 2
    async with Client(create_server(db, config)) as c:
        yield c


async def _scout_job(db, repo: str = "svc-tested") -> str:
    task = Task(job_id="pending", title=f"Scout {repo}", service=repo, agent_role=AgentRole.SCOUT, max_attempts=1)
    job, _ = await db.create_station_job(SCOUT, f"scout {repo}", task)
    return job.id


async def _submit(client, job_id: str, repo: str = "svc-tested", **changes) -> dict:
    result = await client.call_tool("submit_scout_finding", {"job_id": job_id, "repo": repo, **GOOD, **changes})
    return json.loads(result.content[0].text)


@pytest.mark.asyncio
class TestFiling:
    async def test_a_good_finding_becomes_an_inbox_card_with_only_the_scout_label(self, client, db, filed):
        job_id = await _scout_job(db)

        payload = await _submit(client, job_id)

        assert payload.get("filed") is True, payload
        assert filed[0]["list"] == "Inbox"
        assert filed[0]["labels"] == ["source:scout"], "the minion label would make it claimable by the line"
        assert "How to prove it is done" in filed[0]["description"]
        assert await db.count_filed_scout_findings(job_id) == 1

    async def test_the_scout_never_files_into_on_deck(self, client, db, filed):
        """scout_autoqueue is on, but autoqueue means weight-eligible cards go to
        On-deck -- and there is no weight yet. Until there is, nothing a scout
        files may land in front of the line."""
        job_id = await _scout_job(db)
        for i in range(2):
            await _submit(client, job_id, fingerprint=f"hot_spot:src/f{i}.py")

        lanes = {c["list"].lower() for c in filed}
        assert lanes == {"inbox"}
        assert not any(label.lower() == "minion" for c in filed for label in c["labels"])


@pytest.mark.asyncio
class TestRefusals:
    async def test_no_oracle_is_refused_and_files_nothing(self, client, db, filed):
        job_id = await _scout_job(db)

        payload = await _submit(client, job_id, oracle="tests pass")

        assert payload["retryable"] is True
        assert "process, not the outcome" in payload["error"]
        assert filed == []

    async def test_no_evidence_is_refused(self, client, db, filed):
        job_id = await _scout_job(db)
        payload = await _submit(client, job_id, evidence=[])
        assert "no evidence" in payload["error"]
        assert filed == []

    async def test_a_fingerprint_filed_in_the_last_90_days_is_refused(self, client, db, filed):
        first = await _scout_job(db)
        assert (await _submit(client, first)).get("filed") is True

        later = await _scout_job(db)
        payload = await _submit(client, later)

        assert "already filed" in payload["error"]
        assert payload["retryable"] is False
        assert len(filed) == 1

    async def test_a_fingerprint_older_than_90_days_may_be_filed_again(self, client, db, filed):
        import minions.db.postgres as pg

        first = await _scout_job(db)
        await _submit(client, first)
        old = (datetime.now(UTC) - timedelta(days=91)).isoformat()
        async with db._pool.connection() as conn:
            await conn.execute(f"UPDATE {pg.JOB_SCHEMA}.scout_findings SET created_at = %s", (old,))

        later = await _scout_job(db)
        assert (await _submit(client, later)).get("filed") is True

    async def test_a_refused_duplicate_does_not_block_the_original_kind_check(self, client, db, filed):
        """Refusals are recorded for the metric, but only FILED rows count as filed."""
        job_id = await _scout_job(db)
        await _submit(client, job_id, oracle="tests pass")  # refused_invalid row
        assert (await _submit(client, job_id)).get("filed") is True

    async def test_the_run_cap_stops_the_scout(self, client, db, filed):
        job_id = await _scout_job(db)
        for i in range(2):
            assert (await _submit(client, job_id, fingerprint=f"hot_spot:src/f{i}.py")).get("filed") is True

        payload = await _submit(client, job_id, fingerprint="hot_spot:src/f9.py")

        assert "maximum of 2" in payload["error"]
        assert len(filed) == 2

    async def test_an_untested_repo_must_file_its_test_oracle_first(self, client, db, filed):
        job_id = await _scout_job(db, repo="svc-untested")

        refused = await _submit(client, job_id, repo="svc-untested")
        assert "missing_test_oracle" in refused["error"]
        assert filed == []

        first = await _submit(client, job_id, repo="svc-untested", kind="missing_test_oracle", fingerprint="missing_test_oracle:repo")
        assert first.get("filed") is True
        assert (await _submit(client, job_id, repo="svc-untested")).get("filed") is True

    async def test_a_finding_for_another_repo_is_refused(self, client, db, filed):
        job_id = await _scout_job(db, repo="svc-tested")
        payload = await _submit(client, job_id, repo="svc-untested")
        assert "svc-tested" in payload["error"]
        assert filed == []

    async def test_a_non_scout_job_cannot_file(self, client, db, filed, sample_job):
        payload = await _submit(client, sample_job.id)
        assert "not a scout job" in payload["error"]
        assert filed == []

    async def test_refusals_are_recorded_for_the_metric(self, client, db, filed):
        job_id = await _scout_job(db)
        await _submit(client, job_id, oracle="tests pass")
        await _submit(client, job_id)

        outcomes = {(r["kind"], r["outcome"]): r["count"] for r in await db.get_scout_finding_outcomes(days=30)}

        assert outcomes[("hot_spot", "refused_invalid")] == 1
        assert outcomes[("hot_spot", "filed")] == 1

    async def test_the_findings_metric_renders(self, client, db, filed, monkeypatch):
        import minions.dashboard as dash
        from tests.conftest import TEST_PG_URL

        monkeypatch.setattr(dash, "_postgres_url", TEST_PG_URL)
        job_id = await _scout_job(db)
        await _submit(client, job_id)

        payload = await dash._render_metrics()

        assert 'minion_scout_findings_total{kind="hot_spot",outcome="filed"} 1' in payload
