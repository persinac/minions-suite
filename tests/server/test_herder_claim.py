"""External engineer dispatch: the engine publishes, a herder claims.

Job 793821e8 spent $10.66 and merged nothing. Orchestration — spec analyst plus
arbiter, the part minions genuinely does well — was $0.05 of that. The other
$10.61 was inference with no particular reason to be metered against an API key
when a subscription already pays for a stronger model.

So engineer_dispatch="external" makes the engine publish the work and run
nothing, and a herder (a Claude Code session) claims it over MCP and reports
back through report_pr / update_task_status exactly as an in-process agent
would. AgentWorkItem has carried an mcp_url since the K8s work and nothing ever
consumed it; this is that missing consumer.

Two properties carry the whole design:

* a published task has NO agent row, which is precisely what keeps orphan
  recovery off it — every branch there requires an agent to reason about, so a
  task with none is left alone rather than retried out from under the claimer
* an unclaimed item must not stall the job forever, because a queue that looks
  healthy and never moves is the worst failure mode this system has

Exercised through an in-memory FastMCP client, so the tools are invoked the way
a herder invokes them rather than by reaching past the MCP layer.
"""

import inspect
import json

import pytest
from fastmcp import Client

from minions.config import Config
from minions.core.models import Agent, AgentRole, JobStatus, Task, TaskStatus
from minions.server.mcp import create_server


@pytest.fixture(autouse=True)
def registry(monkeypatch):
    """A known registry, independent of whatever projects.yaml is on this box.

    projects.yaml is gitignored and differs between a dev checkout and the
    ConfigMap mounted in-cluster, so resolving against the real one makes these
    tests pass or fail on local configuration rather than on behaviour.
    """
    from minions.project_registry import ProjectConfig, ServiceTarget

    svc = ServiceTarget(
        name="management-api",
        project_id="flippin-balls/management-api",
        git_provider="github",
        repo_path="/repos/management-api",
        clone_url="https://github.com/flippin-balls/management-api.git",
    )
    project = ProjectConfig(name="fbf", project_id="flippin-balls", git_provider="github", services={"management-api": svc})
    monkeypatch.setattr("minions.project_registry.build_registry", lambda *_a, **_k: {"fbf": project})
    return project


@pytest.fixture
async def mcp_client(db):
    server = create_server(db, Config.from_env())
    async with Client(server) as client:
        yield client


async def _call(client, tool: str, args: dict) -> dict:
    result = await client.call_tool(tool, args)
    return json.loads(result.content[0].text)


async def _job_with_engineer_task(db, service: str = "management-api") -> tuple[str, str]:
    job = await db.create_job("Neutralize CSV formula injection in report exports")
    for status in (JobStatus.SPEC_READY, JobStatus.TASKS_CREATED, JobStatus.DEV_IN_PROGRESS):
        await db.update_job_status(job.id, status)
    task = await db.create_task(
        Task(
            job_id=job.id,
            title="Sanitize CSV cells",
            description="Prefix dangerous cells with an apostrophe",
            service=service,
            agent_role=AgentRole.BACKEND_ENGINEER,
        )
    )
    await db.update_task(task.id, status=TaskStatus.IN_PROGRESS)
    return job.id, task.id


class TestClaiming:
    async def test_empty_queue_returns_null_not_an_error(self, mcp_client):
        """A herder polls this constantly. "nothing to do" is a normal answer."""
        payload = await _call(mcp_client, "claim_engineer_work", {"worker": "herder"})

        assert payload["work"] is None

    async def test_a_published_task_is_claimable(self, mcp_client, db):
        job_id, task_id = await _job_with_engineer_task(db)

        payload = await _call(mcp_client, "claim_engineer_work", {"worker": "herder"})

        assert payload["work"] is not None
        assert payload["work"]["task_id"] == task_id
        assert payload["work"]["job_id"] == job_id

    async def test_the_item_carries_everything_needed_to_work(self, mcp_client, db):
        """A herder that has to make extra lookups will make them inconsistently."""
        await _job_with_engineer_task(db)

        work = (await _call(mcp_client, "claim_engineer_work", {"worker": "herder"}))["work"]

        for field in ("task_id", "job_id", "agent_id", "role", "spec", "service", "engine_repo_path", "clone_url", "default_branch"):
            assert field in work, f"work item missing {field}"
        assert work["spec"].startswith("Neutralize CSV")

    async def test_the_engine_path_is_named_for_whose_path_it_is(self, mcp_client, db):
        """It is the engine's checkout inside its own container. The first real
        herder run was on a different machine entirely, where that path does not
        exist — a plain "repo_path" invites working in the wrong directory."""
        await _job_with_engineer_task(db)

        work = (await _call(mcp_client, "claim_engineer_work", {"worker": "herder"}))["work"]

        assert "repo_path" not in work
        assert work["engine_repo_path"] == "/repos/management-api"
        assert work["clone_url"].endswith("management-api.git")

    async def test_claiming_creates_the_agent_row(self, mcp_client, db):
        """Cost and attribution must land in the same tables as an in-process
        run, or the dashboard and the ceilings stop seeing half the work."""
        job_id, task_id = await _job_with_engineer_task(db)

        work = (await _call(mcp_client, "claim_engineer_work", {"worker": "herder"}))["work"]

        agents = [a for a in await db.get_agents_for_job(job_id) if a.task_id == task_id]
        assert len(agents) == 1
        assert agents[0].id == work["agent_id"]
        assert agents[0].status == "running"

    async def test_the_model_records_which_worker_ran_it(self, mcp_client, db):
        """Otherwise a herder run is indistinguishable from an API run in the
        cost tables, and the whole point is comparing the two."""
        job_id, _ = await _job_with_engineer_task(db)

        await _call(mcp_client, "claim_engineer_work", {"worker": "nexus-1"})

        agent = (await db.get_agents_for_job(job_id))[0]
        assert agent.model == "herder:nexus-1"

    async def test_a_claimed_task_is_not_offered_twice(self, mcp_client, db):
        await _job_with_engineer_task(db)

        first = await _call(mcp_client, "claim_engineer_work", {"worker": "a"})
        second = await _call(mcp_client, "claim_engineer_work", {"worker": "b"})

        assert first["work"] is not None
        assert second["work"] is None

    async def test_a_task_already_running_in_process_is_not_claimable(self, mcp_client, db):
        """Otherwise the herder duplicates work the engine is already paying for."""
        job_id, task_id = await _job_with_engineer_task(db)
        await db.create_agent(Agent(job_id=job_id, role=AgentRole.BACKEND_ENGINEER, task_id=task_id, model="claude-opus-5", status="running"))

        assert (await _call(mcp_client, "claim_engineer_work", {"worker": "herder"}))["work"] is None

    async def test_reviewer_tasks_are_not_offered(self, mcp_client, db):
        """This dispatch is scoped to engineers. Reviewers stay in-process until
        the fan-out is proven, because they are the part that works."""
        job = await db.create_job("spec")
        for status in (JobStatus.SPEC_READY, JobStatus.TASKS_CREATED, JobStatus.DEV_IN_PROGRESS):
            await db.update_job_status(job.id, status)
        task = await db.create_task(
            Task(job_id=job.id, title="review", description="d", service="management-api", agent_role=AgentRole.CODE_REVIEWER)
        )
        await db.update_task(task.id, status=TaskStatus.IN_PROGRESS)

        assert (await _call(mcp_client, "claim_engineer_work", {"worker": "herder"}))["work"] is None

    async def test_an_unknown_service_is_skipped_not_crashed(self, mcp_client, db):
        """projects.yaml drifts. A task naming a service the registry does not
        have must not take the whole claim loop down."""
        await _job_with_engineer_task(db, service="not-a-real-service")

        payload = await _call(mcp_client, "claim_engineer_work", {"worker": "herder"})

        assert payload["work"] is None


class TestPeeking:
    """Deciding whether to start a herder must not take the work.

    A trigger polls to answer "is anything waiting". If it polled with
    `claim_engineer_work`, asking would take ownership: the item gets an agent
    row, no worker is coming for it, and `run_engineer`'s fallback leaves it
    alone until `herder_claim_timeout_seconds` expires. Every poll would strand
    a task for fifteen minutes, and the queue would look permanently busy while
    nothing moved.
    """

    async def test_an_empty_queue_reports_zero(self, mcp_client):
        payload = await _call(mcp_client, "peek_engineer_work", {})

        assert payload["count"] == 0
        assert payload["waiting"] == []

    async def test_a_published_task_is_visible(self, mcp_client, db):
        job_id, task_id = await _job_with_engineer_task(db)

        payload = await _call(mcp_client, "peek_engineer_work", {})

        assert payload["count"] == 1
        assert payload["waiting"][0]["task_id"] == task_id
        assert payload["waiting"][0]["job_id"] == job_id

    async def test_peeking_creates_no_agent_row(self, mcp_client, db):
        """The property the whole tool exists for."""
        job_id, task_id = await _job_with_engineer_task(db)

        await _call(mcp_client, "peek_engineer_work", {})
        await _call(mcp_client, "peek_engineer_work", {})

        agents = await db.get_agents_for_job(job_id)
        assert [a for a in agents if a.task_id == task_id] == [], "peek took ownership"

    async def test_peeking_leaves_the_item_claimable(self, mcp_client, db):
        """Peek then claim must still succeed — otherwise polling eats the queue."""
        _, task_id = await _job_with_engineer_task(db)

        await _call(mcp_client, "peek_engineer_work", {})
        claimed = await _call(mcp_client, "claim_engineer_work", {"worker": "herder"})

        assert claimed["work"] is not None
        assert claimed["work"]["task_id"] == task_id

    async def test_peek_and_claim_agree(self, mcp_client, db):
        """They share find_claimable_work so they cannot drift.

        If peek reported work that claim then refused, a trigger would spawn a
        herder into an empty queue and the pane would exit having done nothing —
        repeatedly, since the item stays visible to peek.
        """
        await _job_with_engineer_task(db)

        assert (await _call(mcp_client, "peek_engineer_work", {}))["count"] == 1
        assert (await _call(mcp_client, "claim_engineer_work", {"worker": "herder"}))["work"] is not None
        assert (await _call(mcp_client, "peek_engineer_work", {}))["count"] == 0

    async def test_it_carries_what_a_spawn_needs(self, mcp_client, db):
        """Enough to name the pane and choose a working directory, no more."""
        await _job_with_engineer_task(db)

        item = (await _call(mcp_client, "peek_engineer_work", {}))["waiting"][0]

        for field in ("task_id", "job_id", "role", "service", "clone_url", "is_revision"):
            assert item.get(field) is not None, f"peek item missing {field}"

    async def test_reviewer_tasks_are_not_offered(self, mcp_client, db):
        job = await db.create_job("spec")
        for status in (JobStatus.SPEC_READY, JobStatus.TASKS_CREATED, JobStatus.DEV_IN_PROGRESS):
            await db.update_job_status(job.id, status)
        task = await db.create_task(
            Task(job_id=job.id, title="Review", description="", service="management-api", agent_role=AgentRole.CODE_REVIEWER)
        )
        await db.update_task(task.id, status=TaskStatus.IN_PROGRESS)

        assert (await _call(mcp_client, "peek_engineer_work", {}))["count"] == 0


class TestRegistryIsolation:
    """Guard the fixture that keeps these tests honest.

    The registry fixture patches `minions.project_registry.build_registry`. That
    only works because `find_claimable_work` imports it INSIDE the function — a
    module-level `from … import build_registry` binds the name at import time
    and the patch never reaches it. These tests would then resolve against
    whatever projects.yaml is on the box and pass on local configuration rather
    than on behaviour, which is what the fixture exists to prevent.

    The service name below is deliberately absent from the real projects.yaml,
    so this fails the moment the import moves back to module scope.
    """

    async def test_the_patched_registry_is_the_one_used(self, mcp_client, db, monkeypatch):
        from minions.project_registry import ProjectConfig, ServiceTarget

        svc = ServiceTarget(
            name="zzz-not-in-any-real-config",
            project_id="x/zzz",
            git_provider="github",
            clone_url="https://example.invalid/zzz.git",
        )
        monkeypatch.setattr(
            "minions.project_registry.build_registry",
            lambda *_a, **_k: {"p": ProjectConfig(name="p", project_id="x", git_provider="github", services={svc.name: svc})},
        )
        await _job_with_engineer_task(db, service=svc.name)

        payload = await _call(mcp_client, "peek_engineer_work", {})

        assert payload["count"] == 1, "the patched registry did not reach find_claimable_work"
        assert payload["waiting"][0]["clone_url"] == "https://example.invalid/zzz.git"


class TestCompleting:
    async def test_completing_closes_the_claim(self, mcp_client, db):
        """The first real herder run left its agent "running" forever, which
        makes the shutdown drain wait its full grace period and makes the
        orphaned-checkout reset think the repo is still owned."""
        await _job_with_engineer_task(db)
        work = (await _call(mcp_client, "claim_engineer_work", {"worker": "a"}))["work"]

        payload = await _call(mcp_client, "complete_engineer_work", {"agent_id": work["agent_id"], "summary": "opened PR 84"})

        assert payload["completed"] is True
        agent = await db.get_agent(work["agent_id"])
        assert agent.status == "done"
        assert agent.finished_at

    async def test_zero_cost_is_recorded_not_left_null(self, mcp_client, db):
        """A subscription run genuinely costs nothing, but recording it keeps
        "free by design" distinguishable from "never reported" — the model
        column says herder:<worker>, so the zero is meaningful."""
        await _job_with_engineer_task(db)
        work = (await _call(mcp_client, "claim_engineer_work", {"worker": "a"}))["work"]

        await _call(mcp_client, "complete_engineer_work", {"agent_id": work["agent_id"]})

        agent = await db.get_agent(work["agent_id"])
        assert agent.cost_usd == 0.0
        assert agent.model.startswith("herder:")

    async def test_completing_an_unknown_agent_errors_cleanly(self, mcp_client):
        assert "error" in await _call(mcp_client, "complete_engineer_work", {"agent_id": "nope"})


class TestReleasing:
    async def test_releasing_makes_the_task_claimable_again(self, mcp_client, db):
        """A herder that is rate-limited or interrupted must be able to say so
        rather than going quiet and stranding the task."""
        await _job_with_engineer_task(db)
        work = (await _call(mcp_client, "claim_engineer_work", {"worker": "a"}))["work"]

        released = await _call(mcp_client, "release_engineer_work", {"agent_id": work["agent_id"], "reason": "rate limited"})
        assert released["released"] is True

        assert (await _call(mcp_client, "claim_engineer_work", {"worker": "b"}))["work"] is not None

    async def test_the_reason_is_recorded_on_the_agent(self, mcp_client, db):
        await _job_with_engineer_task(db)
        work = (await _call(mcp_client, "claim_engineer_work", {"worker": "a"}))["work"]

        await _call(mcp_client, "release_engineer_work", {"agent_id": work["agent_id"], "reason": "out of depth"})

        agent = await db.get_agent(work["agent_id"])
        assert agent.status == "failed"
        assert "out of depth" in (agent.error or "")

    async def test_releasing_an_unknown_agent_errors_cleanly(self, mcp_client):
        payload = await _call(mcp_client, "release_engineer_work", {"agent_id": "nope", "reason": "x"})

        assert "error" in payload


class TestEnginePublishesInsteadOfRunning:
    def test_external_dispatch_returns_before_creating_an_agent(self):
        """The absence of an agent row is what keeps orphan recovery off the
        task — every branch there needs an agent to reason about."""
        from minions.engine.dev import run_engineer

        source = inspect.getsource(run_engineer)

        idx_guard = source.index('engine.config.engineer_dispatch == "external"')
        idx_agent = source.index("agent = Agent(job_id=job.id, role=task.agent_role")
        assert idx_guard < idx_agent, "the external branch must return before the agent row is created"

    def test_the_fallback_can_override_it(self):
        from minions.engine.dev import run_engineer

        source = inspect.getsource(run_engineer)
        assert 'engineer_dispatch == "external" and not force_in_process' in source

    def test_an_unclaimed_item_eventually_runs_in_process(self):
        """A herder that is asleep, rate-limited or dead must not stall the job.
        Falling back costs API tokens; not falling back costs the job."""
        from minions.engine.dev import manage_dev_tasks

        source = inspect.getsource(manage_dev_tasks)

        assert "herder_claim_timeout_seconds" in source
        assert "force_in_process=True" in source

    def test_the_fallback_only_applies_to_unowned_tasks(self):
        """ "Unowned" means no agent for the CURRENT attempt, not no agent row.

        A bare `latest_agent is None` was true only on the first attempt: a
        retry re-publishes the item while the previous attempt's agent row
        survives, so the fallback was skipped and the retry was judged against
        its predecessor. See tests/engine/test_retry_gets_herder_fallback.py.
        """
        from minions.engine.dev import manage_dev_tasks

        source = inspect.getsource(manage_dev_tasks)
        assert "latest_agent is None" in source
        assert "unbounded=True" in source


class TestTimestampAgeHelper:
    def test_none_is_zero(self):
        from minions.engine.dev import _seconds_since

        assert _seconds_since(None) == 0.0

    def test_garbage_is_zero_not_an_exception(self):
        """0.0 postpones the fallback. An unreadable timestamp must not trigger
        a duplicate in-process run."""
        from minions.engine.dev import _seconds_since

        assert _seconds_since("not-a-date") == 0.0

    def test_a_past_timestamp_has_positive_age(self):
        from datetime import UTC, datetime, timedelta

        from minions.engine.dev import _seconds_since

        past = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
        assert _seconds_since(past) >= 119

    def test_a_naive_timestamp_is_treated_as_utc(self):
        """The database writes some timestamps without tzinfo; subtracting an
        aware from a naive datetime raises TypeError rather than returning a
        wrong number, so this would be a crash in the poll loop."""
        from datetime import UTC, datetime, timedelta

        from minions.engine.dev import _seconds_since

        naive = (datetime.now(UTC) - timedelta(seconds=60)).replace(tzinfo=None).isoformat()
        assert _seconds_since(naive) >= 59


class TestConfig:
    def test_dispatch_defaults_to_in_process(self):
        """Opt-in. Nobody gets external dispatch by upgrading."""
        assert Config.from_env().engineer_dispatch == "in_process"

    def test_the_claim_timeout_has_a_real_default(self):
        assert Config.from_env().herder_claim_timeout_seconds > 0
