"""External reviewer dispatch: the engine publishes a review, a herder claims it.

Reviewers are ~40% of spend metered against the API key — second only to
engineers (~46%), and the last big share that a subscription-backed session
could have been doing instead. They could not be claimed for one concrete
reason: both reviewer launch paths created the agent row immediately, and an
agent row is exactly what `find_claimable_work` reads as "somebody already owns
this".

`reviewer_dispatch="external"` flips that. These tests pin the MCP half:

* the role gate widens with the config and NOT otherwise, so upgrading changes
  nothing until an operator sets the env var
* `find_claimable_work` stays ONE definition — peek and claim must agree, or a
  trigger spawns herders for items that cannot be claimed and the queue looks
  permanently busy while nothing moves
* a claimed reviewer can actually SEE the code. A reviewer whose file tools
  point at a directory that does not exist gets "no such file" from read_file
  and `[]` from list_files — indistinguishable from an empty repo — and returns
  a confident verdict on a diff it never read (job 7ba724fd)
* closing a reviewer claim REQUIRES a verdict, because the fan-out reads an
  absent one as a silent reviewer and fails closed into a revision nobody asked
  for

Exercised through an in-memory FastMCP client, so the tools are invoked the way
a herder invokes them rather than by reaching past the MCP layer.
"""

import dataclasses
import json

import pytest
from fastmcp import Client

from minions.config import Config
from minions.core.models import Agent, AgentRole, JobStatus, Task, TaskStatus
from minions.server.mcp import create_server

PR_URL = "https://github.com/flippin-balls/management-api/pull/84"


@pytest.fixture(autouse=True)
def registry(monkeypatch):
    """A known registry, independent of whatever projects.yaml is on this box.

    Same reasoning as tests/server/test_herder_claim.py: projects.yaml is
    gitignored and differs between a dev checkout and the ConfigMap mounted
    in-cluster, so resolving against the real one makes these tests pass or fail
    on local configuration rather than on behaviour.

    The project is named "fbf" and its service "management-api" — deliberately
    different strings, because a review-type job writes the PROJECT name into
    task.service while a dev job writes the SERVICE name, and a fixture where
    the two matched would hide that.
    """
    from minions.project_registry import ProjectConfig, ServiceTarget

    svc = ServiceTarget(
        name="management-api",
        project_id="flippin-balls/management-api",
        git_provider="github",
        repo_path="/repos/management-api",
        clone_url="https://github.com/flippin-balls/management-api.git",
    )
    project = ProjectConfig(
        name="fbf",
        project_id="flippin-balls",
        git_provider="github",
        repo_path="/repos/fbf",
        services={"management-api": svc},
    )
    monkeypatch.setattr("minions.project_registry.build_registry", lambda *_a, **_k: {"fbf": project})
    return project


def _config(**overrides) -> Config:
    return dataclasses.replace(Config.from_env(), **overrides)


@pytest.fixture
async def external_client(db):
    """An MCP server configured for external reviewer dispatch."""
    server = create_server(db, _config(reviewer_dispatch="external"))
    async with Client(server) as client:
        yield client


@pytest.fixture
async def in_process_client(db):
    """The default. Nothing here should offer a reviewer item."""
    server = create_server(db, _config(reviewer_dispatch="in_process"))
    async with Client(server) as client:
        yield client


async def _call(client, tool: str, args: dict) -> dict:
    result = await client.call_tool(tool, args)
    return json.loads(result.content[0].text)


async def _dev_job_with_reviewer_task(db, *, specialty: str = "api", revision_count: int = 0) -> tuple[str, str]:
    """A dev job whose engineer opened a PR, with one published reviewer item.

    Mirrors what `_run_one_specialist` leaves behind under external dispatch:
    a CODE_REVIEWER task, IN_PROGRESS, carrying the PR, with no agent row.
    """
    job = await db.create_job("Neutralize CSV formula injection in report exports")
    for status in (JobStatus.SPEC_READY, JobStatus.TASKS_CREATED, JobStatus.DEV_IN_PROGRESS):
        await db.update_job_status(job.id, status)
    task = await db.create_task(
        Task(
            job_id=job.id,
            title=f"[{specialty}] Review PR for Sanitize CSV cells",
            description=f"Review PR {PR_URL}",
            service="management-api",
            agent_role=AgentRole.CODE_REVIEWER,
            specialty=specialty,
            revision_count=revision_count,
            mr_url=PR_URL,
            mr_id="84",
            pr_url=PR_URL,
            pr_number=84,
        )
    )
    await db.update_task(task.id, status=TaskStatus.IN_PROGRESS)
    return job.id, task.id


async def _review_job(db, project: str = "fbf") -> tuple[str, str]:
    """A standalone review-type job, as an MR webhook or the CLI creates it."""
    job, task = await db.create_review_job(project, PR_URL, "84")
    await db.update_task(task.id, status=TaskStatus.IN_PROGRESS)
    return job.id, task.id


async def _engineer_task(db) -> tuple[str, str]:
    job = await db.create_job("spec")
    for status in (JobStatus.SPEC_READY, JobStatus.TASKS_CREATED, JobStatus.DEV_IN_PROGRESS):
        await db.update_job_status(job.id, status)
    task = await db.create_task(
        Task(job_id=job.id, title="Sanitize CSV cells", description="d", service="management-api", agent_role=AgentRole.BACKEND_ENGINEER)
    )
    await db.update_task(task.id, status=TaskStatus.IN_PROGRESS)
    return job.id, task.id


class TestTheGateIsTheConfig:
    """Merging this must change production behaviour not at all.

    Every assertion here is about the DEFAULT being inert. If the role gate ever
    stops consulting reviewer_dispatch, these fail and the `external` ones still
    pass — which is the asymmetry that makes them worth writing separately.
    """

    def test_reviewer_dispatch_defaults_to_in_process(self):
        assert Config.from_env().reviewer_dispatch == "in_process"

    def test_the_env_var_is_read(self, monkeypatch):
        monkeypatch.setenv("REVIEWER_DISPATCH", "external")

        assert Config.from_env().reviewer_dispatch == "external"

    async def test_by_default_a_reviewer_task_is_not_claimable(self, in_process_client, db):
        await _dev_job_with_reviewer_task(db)

        assert (await _call(in_process_client, "claim_engineer_work", {"worker": "herder"}))["work"] is None

    async def test_by_default_a_reviewer_task_is_not_even_visible(self, in_process_client, db):
        await _dev_job_with_reviewer_task(db)

        assert (await _call(in_process_client, "peek_engineer_work", {}))["count"] == 0

    async def test_by_default_a_review_job_is_not_claimable(self, in_process_client, db):
        await _review_job(db)

        assert (await _call(in_process_client, "claim_engineer_work", {"worker": "herder"}))["work"] is None

    async def test_engineers_are_unaffected_by_the_reviewer_knob(self, external_client, db):
        """Widening the gate must not narrow it. An engineer item stays claimable
        whatever reviewer_dispatch says."""
        _, task_id = await _engineer_task(db)

        work = (await _call(external_client, "claim_engineer_work", {"worker": "herder"}))["work"]

        assert work is not None
        assert work["task_id"] == task_id

    async def test_no_config_at_all_means_engineers_only(self, db):
        """find_claimable_work accepts config=None. That path predates this
        change and must keep its old answer rather than guessing."""
        from minions.server.mcp import find_claimable_work

        await _dev_job_with_reviewer_task(db)

        assert await find_claimable_work(db, None) == []


class TestClaimingAReviewerItem:
    async def test_a_published_reviewer_task_is_claimable(self, external_client, db):
        job_id, task_id = await _dev_job_with_reviewer_task(db)

        work = (await _call(external_client, "claim_engineer_work", {"worker": "herder"}))["work"]

        assert work is not None
        assert work["task_id"] == task_id
        assert work["job_id"] == job_id
        assert work["role"] == "code_reviewer"

    async def test_a_standalone_review_job_is_claimable(self, external_client, db):
        """create_review_job writes the PROJECT name into task.service, not a
        service name. Scanning only the services map dropped every review job on
        the floor with a "no service in registry" warning — which reads like
        projects.yaml drift rather than a shape mismatch."""
        job_id, task_id = await _review_job(db)

        work = (await _call(external_client, "claim_engineer_work", {"worker": "herder"}))["work"]

        assert work is not None, "a review-type job's task was not resolvable"
        assert work["task_id"] == task_id
        assert work["job_id"] == job_id
        assert work["project"] == "fbf"

    async def test_an_engineer_task_naming_an_unknown_service_is_still_skipped(self, external_client, db):
        """The project fallback above is scoped to reviewers on purpose. An
        engineer task naming a service that does not exist must stay skipped
        rather than resolve to some project that happens to share the name."""
        job = await db.create_job("spec")
        for status in (JobStatus.SPEC_READY, JobStatus.TASKS_CREATED, JobStatus.DEV_IN_PROGRESS):
            await db.update_job_status(job.id, status)
        task = await db.create_task(
            Task(job_id=job.id, title="t", description="d", service="fbf", agent_role=AgentRole.BACKEND_ENGINEER),
        )
        await db.update_task(task.id, status=TaskStatus.IN_PROGRESS)

        assert (await _call(external_client, "claim_engineer_work", {"worker": "herder"}))["work"] is None

    async def test_claiming_creates_the_agent_row(self, external_client, db):
        job_id, task_id = await _dev_job_with_reviewer_task(db)

        work = (await _call(external_client, "claim_engineer_work", {"worker": "nexus-1"}))["work"]

        agents = [a for a in await db.get_agents_for_job(job_id) if a.task_id == task_id]
        assert len(agents) == 1
        assert agents[0].id == work["agent_id"]
        assert agents[0].model == "herder:nexus-1"
        assert agents[0].role == AgentRole.CODE_REVIEWER

    async def test_a_claimed_reviewer_task_is_not_offered_twice(self, external_client, db):
        await _dev_job_with_reviewer_task(db)

        first = await _call(external_client, "claim_engineer_work", {"worker": "a"})
        second = await _call(external_client, "claim_engineer_work", {"worker": "b"})

        assert first["work"] is not None
        assert second["work"] is None

    async def test_a_reviewer_already_running_in_process_is_not_claimable(self, external_client, db):
        """Otherwise the herder duplicates a review the engine is already paying
        for, and two panels vote on one PR."""
        job_id, task_id = await _dev_job_with_reviewer_task(db)
        await db.create_agent(Agent(job_id=job_id, role=AgentRole.CODE_REVIEWER, task_id=task_id, model="claude-sonnet-5", status="running"))

        assert (await _call(external_client, "claim_engineer_work", {"worker": "herder"}))["work"] is None


class TestTheClaimedReviewerCanSeeTheCode:
    """The named regression. A reviewer that cannot read the diff still returns
    a verdict — confidently, and on nothing."""

    async def test_it_carries_the_pr_reference(self, external_client, db):
        await _dev_job_with_reviewer_task(db)

        work = (await _call(external_client, "claim_engineer_work", {"worker": "herder"}))["work"]

        assert work["mr_url"] == PR_URL
        assert work["mr_id"] == "84"
        assert work["pr_url"] == PR_URL
        assert work["pr_number"] == 84

    async def test_it_carries_the_repo_coordinates(self, external_client, db):
        await _dev_job_with_reviewer_task(db)

        work = (await _call(external_client, "claim_engineer_work", {"worker": "herder"}))["work"]

        assert work["project_id"] == "flippin-balls/management-api"
        assert work["git_provider"] == "github"
        assert work["clone_url"].endswith("management-api.git")

    async def test_it_carries_the_specialty_and_its_persona(self, external_client, db):
        """A panel of five identical reviewers is not a panel. The lens is the
        whole reason the fan-out costs what it costs."""
        await _dev_job_with_reviewer_task(db, specialty="dba")

        work = (await _call(external_client, "claim_engineer_work", {"worker": "herder"}))["work"]

        assert work["specialty"] == "dba"
        assert work["persona"], "the dba persona did not travel with the work item"

    async def test_a_specialty_with_no_persona_file_degrades_rather_than_crashes(self, external_client, db):
        await _dev_job_with_reviewer_task(db, specialty="no-such-lens")

        work = (await _call(external_client, "claim_engineer_work", {"worker": "herder"}))["work"]

        assert work is not None
        assert work["persona"] == ""

    async def test_the_instructions_say_how_to_get_the_diff(self, external_client, db):
        """Not decoration. `engine_repo_path` is the engine's path inside its own
        container and the first real herder run was on another machine entirely."""
        await _dev_job_with_reviewer_task(db)

        work = (await _call(external_client, "claim_engineer_work", {"worker": "herder"}))["work"]

        instructions = work["review_instructions"]
        assert "diff" in instructions
        assert "clone_url" in instructions
        assert "engine_repo_path" in instructions, "nothing warns the herder off the engine's own path"
        assert "release_engineer_work" in instructions, "no stated exit for a reviewer that cannot read the code"

    async def test_an_engineer_gets_no_review_instructions(self, external_client, db):
        """Role-shaped payload. An engineer told to 'review through the lens in
        persona' would be a prompt-injection of our own making."""
        await _engineer_task(db)

        work = (await _call(external_client, "claim_engineer_work", {"worker": "herder"}))["work"]

        assert work["review_instructions"] == ""
        assert work["persona"] == ""
        assert work["specialty"] == ""

    async def test_a_re_review_round_is_not_handed_the_engineers_checklist(self, external_client, db):
        """get_review_feedback renders findings as "you MUST account for EVERY
        one" — an instruction to the AUTHOR. A reviewer on revision 2 satisfies
        the is_revision condition, so without a role guard it would be told to
        go fix the code it was asked to judge."""
        await _dev_job_with_reviewer_task(db, revision_count=2)

        work = (await _call(external_client, "claim_engineer_work", {"worker": "herder"}))["work"]

        assert work["is_revision"] is True
        assert work["review_feedback"] == ""


class TestPeekAndClaimAgree:
    """One definition, or the trigger spawns herders into an empty queue."""

    async def test_a_published_reviewer_item_is_visible(self, external_client, db):
        _, task_id = await _dev_job_with_reviewer_task(db, specialty="pythonista")

        payload = await _call(external_client, "peek_engineer_work", {})

        assert payload["count"] == 1
        assert payload["waiting"][0]["task_id"] == task_id
        assert payload["waiting"][0]["role"] == "code_reviewer"
        assert payload["waiting"][0]["specialty"] == "pythonista"

    async def test_peeking_creates_no_agent_row(self, external_client, db):
        job_id, task_id = await _dev_job_with_reviewer_task(db)

        await _call(external_client, "peek_engineer_work", {})
        await _call(external_client, "peek_engineer_work", {})

        assert [a for a in await db.get_agents_for_job(job_id) if a.task_id == task_id] == [], "peek took ownership"

    async def test_peek_then_claim_then_peek(self, external_client, db):
        await _dev_job_with_reviewer_task(db)

        assert (await _call(external_client, "peek_engineer_work", {}))["count"] == 1
        assert (await _call(external_client, "claim_engineer_work", {"worker": "herder"}))["work"] is not None
        assert (await _call(external_client, "peek_engineer_work", {}))["count"] == 0

    async def test_they_share_one_scanner(self):
        """Structural, because the drift this prevents is invisible in any single
        run: the two tools must call the same function, not two that agree today."""
        import inspect

        from minions.server import mcp as mcp_mod

        source = inspect.getsource(mcp_mod.create_server)
        assert source.count("await find_claimable_work(db, config)") == 2, "peek and claim no longer share find_claimable_work"


class TestClosingAReviewerClaim:
    async def test_a_verdict_closes_the_task_and_the_claim(self, external_client, db):
        """The engine polls the TASK, so closing only the agent would leave the
        review invisible and the PR waiting on a worker that had finished."""
        _, task_id = await _dev_job_with_reviewer_task(db)
        work = (await _call(external_client, "claim_engineer_work", {"worker": "a"}))["work"]

        payload = await _call(external_client, "complete_engineer_work", {"agent_id": work["agent_id"], "verdict": "approve"})

        assert payload["completed"] is True
        assert payload["verdict"] == "approve"
        agent = await db.get_agent(work["agent_id"])
        assert agent.status == "done"
        task = await db.get_task(task_id)
        assert task.status == TaskStatus.DONE
        assert task.verdict == "approve"

    @pytest.mark.parametrize(
        ("spelling", "canonical"),
        [
            ("approve", "approve"),
            ("approved", "approve"),
            ("APPROVE", "approve"),
            ("request_changes", "request_changes"),
            ("changes_requested", "request_changes"),
            ("request-changes", "request_changes"),
        ],
    )
    async def test_the_spellings_in_circulation_are_accepted(self, external_client, db, spelling, canonical):
        """report_review_complete already lost a review to this exact mismatch —
        its schema advertised one spelling and its check wanted another."""
        _, task_id = await _dev_job_with_reviewer_task(db)
        work = (await _call(external_client, "claim_engineer_work", {"worker": "a"}))["work"]

        payload = await _call(external_client, "complete_engineer_work", {"agent_id": work["agent_id"], "verdict": spelling})

        assert payload.get("verdict") == canonical
        assert (await db.get_task(task_id)).verdict == canonical

    async def test_closing_a_reviewer_without_a_verdict_is_refused(self, external_client, db):
        """The whole point. The fan-out reads an absent verdict as a SILENT
        reviewer, which buys one re-run and then fails closed into a revision
        nobody asked for — so a forgotten argument would launder "I finished"
        into "I objected"."""
        _, task_id = await _dev_job_with_reviewer_task(db)
        work = (await _call(external_client, "claim_engineer_work", {"worker": "a"}))["work"]

        payload = await _call(external_client, "complete_engineer_work", {"agent_id": work["agent_id"], "summary": "looked at it"})

        assert "error" in payload
        assert (await db.get_task(task_id)).status == TaskStatus.IN_PROGRESS

    async def test_a_refused_close_leaves_the_claim_open_for_a_retry(self, external_client, db):
        """Refusing AFTER closing the agent would strand the task: IN_PROGRESS
        with a finished agent is neither owned nor free, and no recovery path
        reads it."""
        _, task_id = await _dev_job_with_reviewer_task(db)
        work = (await _call(external_client, "claim_engineer_work", {"worker": "a"}))["work"]

        await _call(external_client, "complete_engineer_work", {"agent_id": work["agent_id"]})
        assert (await db.get_agent(work["agent_id"])).status == "running"

        retry = await _call(external_client, "complete_engineer_work", {"agent_id": work["agent_id"], "verdict": "request_changes"})
        assert retry["completed"] is True
        assert (await db.get_task(task_id)).verdict == "request_changes"

    async def test_an_unusable_verdict_is_refused_rather_than_guessed(self, external_client, db):
        _, task_id = await _dev_job_with_reviewer_task(db)
        work = (await _call(external_client, "claim_engineer_work", {"worker": "a"}))["work"]

        payload = await _call(external_client, "complete_engineer_work", {"agent_id": work["agent_id"], "verdict": "looks fine to me"})

        assert "error" in payload
        assert (await db.get_task(task_id)).verdict in (None, "")

    async def test_an_engineer_still_completes_with_no_verdict(self, external_client, db):
        """The requirement is role-scoped. Every herder pane already running
        calls this tool with three arguments."""
        await _engineer_task(db)
        work = (await _call(external_client, "claim_engineer_work", {"worker": "a"}))["work"]

        payload = await _call(external_client, "complete_engineer_work", {"agent_id": work["agent_id"], "summary": "opened PR 84"})

        assert payload["completed"] is True
        assert (await db.get_agent(work["agent_id"])).status == "done"

    async def test_an_engineers_task_is_not_closed_by_completing(self, external_client, db):
        """report_pr owns the engineer task's state. Marking it DONE here would
        skip review entirely."""
        _, task_id = await _engineer_task(db)
        work = (await _call(external_client, "claim_engineer_work", {"worker": "a"}))["work"]

        await _call(external_client, "complete_engineer_work", {"agent_id": work["agent_id"]})

        assert (await db.get_task(task_id)).status == TaskStatus.IN_PROGRESS

    async def test_feedback_is_kept_as_a_message(self, external_client, db):
        job_id, _ = await _dev_job_with_reviewer_task(db)
        work = (await _call(external_client, "claim_engineer_work", {"worker": "a"}))["work"]

        await _call(
            external_client,
            "complete_engineer_work",
            {"agent_id": work["agent_id"], "verdict": "request_changes", "feedback": "HIGH — unbounded query in report export"},
        )

        messages = await db.get_messages(job_id)
        assert any("unbounded query" in m.content for m in messages)

    async def test_the_verdict_is_recorded_as_an_event(self, external_client, db):
        """A completion that does not say what was decided is unreadable in the
        audit trail, and the audit trail is how a herder run is told from an API
        one after the fact."""
        job_id, _ = await _dev_job_with_reviewer_task(db)
        work = (await _call(external_client, "claim_engineer_work", {"worker": "a"}))["work"]

        await _call(external_client, "complete_engineer_work", {"agent_id": work["agent_id"], "verdict": "approve"})

        completed = [e for e in await db.get_events(job_id) if e["event_type"] == "work_item_completed"]
        assert len(completed) == 1
        assert "verdict=approve" in completed[0]["detail"]


class TestReleasingAReviewerClaim:
    async def test_releasing_makes_the_review_claimable_again(self, external_client, db):
        """The stated exit for a reviewer that could not read the code. A verdict
        from one that never saw the diff is worse than no reviewer at all."""
        _, task_id = await _dev_job_with_reviewer_task(db)
        work = (await _call(external_client, "claim_engineer_work", {"worker": "a"}))["work"]

        released = await _call(external_client, "release_engineer_work", {"agent_id": work["agent_id"], "reason": "could not fetch the diff"})
        assert released["released"] is True

        again = await _call(external_client, "claim_engineer_work", {"worker": "b"})
        assert again["work"] is not None
        assert again["work"]["task_id"] == task_id

    async def test_releasing_records_no_verdict(self, external_client, db):
        _, task_id = await _dev_job_with_reviewer_task(db)
        work = (await _call(external_client, "claim_engineer_work", {"worker": "a"}))["work"]

        await _call(external_client, "release_engineer_work", {"agent_id": work["agent_id"], "reason": "rate limited"})

        task = await db.get_task(task_id)
        assert task.status == TaskStatus.IN_PROGRESS
        assert task.verdict in (None, "")


class TestHerderStatusSeesReviewers:
    async def test_a_live_reviewer_claim_is_reported(self, external_client, db):
        """The trigger's reaper closes a pane when its claim is done. A reviewer
        pane invisible to it would be reaped while working, or never."""
        _, task_id = await _dev_job_with_reviewer_task(db)
        work = (await _call(external_client, "claim_engineer_work", {"worker": "nexus-2"}))["work"]

        live = (await _call(external_client, "herder_status", {}))["live"]

        assert [entry["task_id"] for entry in live] == [task_id]
        assert live[0]["worker"] == "nexus-2"
        assert live[0]["agent_id"] == work["agent_id"]

    async def test_a_completed_reviewer_claim_disappears(self, external_client, db):
        await _dev_job_with_reviewer_task(db)
        work = (await _call(external_client, "claim_engineer_work", {"worker": "nexus-2"}))["work"]

        await _call(external_client, "complete_engineer_work", {"agent_id": work["agent_id"], "verdict": "approve"})

        assert (await _call(external_client, "herder_status", {}))["live"] == []
