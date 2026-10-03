"""The scout station's lifecycle: when it runs, where, and what it may touch.

Real DB, real Config, real git for the signals phase. The engine is a thin fake
that records what would have been launched instead of calling a model, so every
assertion here is about a decision the scout made, not about a model's output.
"""

import json
import subprocess
from datetime import UTC, datetime, timedelta

import pytest

from minions.agents.tools.mcp_executor import McpToolExecutor
from minions.config import Config
from minions.core.models import Agent, AgentRole, JobStatus, TaskStatus
from minions.core.stations import SCOUT, StationBudgetGuard
from minions.engine import scout
from minions.project_registry import ProjectConfig, ServiceTarget


def _service(name: str, test_command: str = "uv run pytest") -> ServiceTarget:
    return ServiceTarget(name=name, clone_url=f"https://example.invalid/{name}.git", test_command=test_command)


def _registry(*services: ServiceTarget, project: str = "proj") -> dict:
    return {project: ProjectConfig(name=project, project_id=f"org/{project}", services={s.name: s for s in services})}


class _Engine:
    """What scout.py touches on JobEngine, and nothing else."""

    def __init__(self, db, config: Config, registry: dict):
        self.db = db
        self.config = config
        self.registry = registry
        self.station_guard = StationBudgetGuard()
        self.spawned: list[dict] = []

    def _resolve_service(self, name):
        for project in self.registry.values():
            if name in project.services:
                return project, project.services[name]
        return None, None

    def _run_in_process(self, job, task, agent, project, service, context=None, knowledge_context=None, cost_limit_usd=None):
        # Sync on purpose: _spawn receives this dict instead of a coroutine, so
        # the launch is recorded without any model being called.
        return {"job": job, "task": task, "agent": agent, "service": service, "context": context, "cost_limit_usd": cost_limit_usd}

    def _spawn(self, launch, name):
        self.spawned.append({**launch, "name": name})


def _config(tmp_path, **overrides) -> Config:
    config = Config.from_env()
    config.scout_enabled = True
    config.scout_max_findings = 3
    config.scout_checkout_dir = str(tmp_path / "scout")
    config.role_models = {SCOUT: "claude-sonnet-5"}
    config.station_budgets = {SCOUT: {"daily_usd": 2.0, "max_runs_per_day": 3}}
    config.station_total_daily_usd = 3.0
    config.scout_exclude_repos = ("infrastructure", "esp-cryptoauthlib")
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


async def _set_created_at(db, job_id: str, when: datetime) -> None:
    import minions.db.postgres as pg

    async with db._pool.connection() as conn:
        await conn.execute(f"UPDATE {pg.JOB_SCHEMA}.jobs SET created_at = %s WHERE id = %s", (when.isoformat(), job_id))


@pytest.mark.asyncio
class TestScheduling:
    async def test_a_due_run_creates_one_scout_job(self, db, tmp_path):
        engine = _Engine(db, _config(tmp_path), _registry(_service("wallet-api")))

        job = await scout.scout_tick(engine)

        assert job is not None
        assert job.job_type == SCOUT
        assert job.status == JobStatus.TASKS_CREATED
        tasks = await db.get_tasks(job.id)
        assert [(t.agent_role, t.service) for t in tasks] == [(AgentRole.SCOUT, "wallet-api")]

    async def test_the_kill_switch_schedules_nothing(self, db, tmp_path):
        engine = _Engine(db, _config(tmp_path, scout_enabled=False), _registry(_service("wallet-api")))
        assert await scout.scout_tick(engine) is None

    async def test_only_one_scout_runs_at_a_time(self, db, tmp_path):
        engine = _Engine(
            db, _config(tmp_path, station_budgets={SCOUT: {"daily_usd": 2.0, "max_runs_per_day": 99}}), _registry(_service("a"), _service("b"))
        )
        first = await scout.scout_tick(engine)
        await _set_created_at(db, first.id, datetime.now(UTC) - timedelta(hours=12))

        assert await scout.scout_tick(engine) is None

    async def test_runs_are_spread_across_the_day(self, db, tmp_path):
        """3 runs a day means one per 8h, not three in the first ten minutes."""
        engine = _Engine(db, _config(tmp_path), _registry(_service("a"), _service("b")))
        first = await scout.scout_tick(engine)
        await db.update_job_status(first.id, JobStatus.FAILED, error="done with it")
        now = datetime.now(UTC)

        assert await scout.scout_tick(engine, now=now + timedelta(hours=7)) is None
        assert await scout.scout_tick(engine, now=now + timedelta(hours=8, minutes=1)) is not None

    async def test_an_exhausted_budget_schedules_nothing(self, db, tmp_path):
        engine = _Engine(db, _config(tmp_path, station_budgets={SCOUT: {"daily_usd": 2.0, "max_runs_per_day": 0}}), _registry(_service("a")))

        assert await scout.scout_tick(engine) is None
        assert await db.get_last_station_job_at(SCOUT) is None

    async def test_missing_tables_keep_the_scout_off(self, db, tmp_path, monkeypatch):
        """Code ships before its migration is applied to the deployed DB."""
        engine = _Engine(db, _config(tmp_path), _registry(_service("a")))

        async def no_tables():
            return False

        monkeypatch.setattr(db, "scout_tables_exist", no_tables)

        assert await scout.scout_tick(engine) is None


class TestRepoChoice:
    def test_excluded_repos_are_never_visited(self, tmp_path):
        config = _config(tmp_path)
        registry = _registry(_service("infrastructure"), _service("wallet-api"))
        registry["esp-cryptoauthlib"] = ProjectConfig(name="esp-cryptoauthlib", project_id="x", services={"lib": _service("lib")})

        names = {s for _, s, _ in scout.eligible_services(registry, config)}

        assert names == {"wallet-api"}, "matched on project name or service name"

    def test_a_repo_with_no_clone_url_is_skipped(self, tmp_path):
        registry = _registry(ServiceTarget(name="nowhere"), _service("wallet-api"))
        assert {s for _, s, _ in scout.eligible_services(registry, _config(tmp_path))} == {"wallet-api"}

    def test_never_scouted_comes_first_then_oldest(self):
        candidates = [("p", "a", None), ("p", "b", None), ("p", "c", None)]
        last = {"a": "2026-10-01T00:00:00+00:00", "b": "2026-09-01T00:00:00+00:00"}

        assert scout.pick_next(candidates, last)[1] == "c"
        assert scout.pick_next(candidates[:2], last)[1] == "b"


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def fake_checkout(monkeypatch):
    """ensure_checkout that builds a small real repo instead of cloning."""
    calls: list[dict] = []

    async def fake(clone_url, dest, default_branch="main", reset_dirty=False):
        calls.append({"dest": dest, "reset_dirty": reset_dirty})
        from pathlib import Path

        root = Path(dest)
        root.mkdir(parents=True, exist_ok=True)
        _git(root, "init", "-q", "-b", "main")
        _git(root, "config", "user.email", "t@example.com")
        _git(root, "config", "user.name", "t")
        (root / "app.py").write_text("# TODO: handle errors\nx = 1\n")
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "c")
        return True

    monkeypatch.setattr(scout, "ensure_checkout", fake)
    return calls


@pytest.mark.asyncio
class TestRun:
    async def test_signals_then_a_launch_on_the_pinned_model_in_the_scouts_own_tree(self, db, tmp_path, fake_checkout):
        config = _config(tmp_path)
        engine = _Engine(db, config, _registry(_service("wallet-api")))
        job = await scout.scout_tick(engine)

        await scout.advance_scout_job(engine, await db.get_job(job.id))

        assert (await db.get_job(job.id)).status == JobStatus.SCOUTING
        launch = engine.spawned[0]
        assert launch["agent"].model == "claude-sonnet-5"
        assert launch["agent"].role == AgentRole.SCOUT
        assert launch["service"].repo_path == str(tmp_path / "scout" / "wallet-api")
        assert fake_checkout[0]["dest"].startswith(str(tmp_path / "scout")), "never an engineer's checkout"
        assert "## Scout signals" in launch["context"]
        assert 0 < launch["cost_limit_usd"] <= 2.0, "one run cannot spend past the station's daily budget"

    async def test_signals_are_recorded_before_any_model_call(self, db, tmp_path, fake_checkout):
        import minions.db.postgres as pg

        engine = _Engine(db, _config(tmp_path), _registry(_service("wallet-api")))
        job = await scout.scout_tick(engine)
        await scout.advance_scout_job(engine, await db.get_job(job.id))

        async with db._pool.connection() as conn:
            cur = await conn.execute(f"SELECT repo, signals FROM {pg.JOB_SCHEMA}.scout_signals WHERE job_id = %s", (job.id,))
            row = await cur.fetchone()
        signals = row["signals"] if isinstance(row["signals"], dict) else json.loads(row["signals"])
        assert row["repo"] == "wallet-api"
        assert signals["todos"] == [{"path": "app.py", "todos": 1}]

    async def test_signals_that_point_nowhere_end_the_run_without_spending(self, db, tmp_path, fake_checkout, monkeypatch):
        engine = _Engine(db, _config(tmp_path), _registry(_service("wallet-api")))
        job = await scout.scout_tick(engine)

        async def empty(repo_dir, repo, service):
            return {"repo": repo, "churn_days": 90, "hot_spots": [], "todos": [], "missing_test_oracle": False, "deferred": []}

        monkeypatch.setattr(scout, "collect_signals", empty)

        await scout.advance_scout_job(engine, await db.get_job(job.id))

        assert (await db.get_job(job.id)).status == JobStatus.DONE
        assert engine.spawned == []

    async def test_spend_is_rechecked_at_the_moment_of_launch(self, db, tmp_path, fake_checkout):
        """Admitted under budget, but spend moved before the model call."""
        engine = _Engine(db, _config(tmp_path), _registry(_service("wallet-api")))
        job = await scout.scout_tick(engine)
        await db.create_agent(Agent(job_id=job.id, role=AgentRole.SCOUT, model="m", status="done", cost_usd=2.5, num_turns=4))

        await scout.advance_scout_job(engine, await db.get_job(job.id))

        assert engine.spawned == []
        assert (await db.get_job(job.id)).status == JobStatus.FAILED

    async def test_the_job_closes_when_its_agent_does(self, db, tmp_path, fake_checkout):
        engine = _Engine(db, _config(tmp_path), _registry(_service("wallet-api")))
        job = await scout.scout_tick(engine)
        await scout.advance_scout_job(engine, await db.get_job(job.id))
        agent = engine.spawned[0]["agent"]

        await scout.advance_scout_job(engine, await db.get_job(job.id))
        assert (await db.get_job(job.id)).status == JobStatus.SCOUTING, "still running"

        await db.update_agent(agent.id, status="done")
        await scout.advance_scout_job(engine, await db.get_job(job.id))

        assert (await db.get_job(job.id)).status == JobStatus.DONE
        assert (await db.get_tasks(job.id))[0].status == TaskStatus.DONE

    async def test_a_failed_agent_fails_the_run_without_a_retry(self, db, tmp_path, fake_checkout):
        engine = _Engine(db, _config(tmp_path), _registry(_service("wallet-api")))
        job = await scout.scout_tick(engine)
        await scout.advance_scout_job(engine, await db.get_job(job.id))
        await db.update_agent(engine.spawned[0]["agent"].id, status="failed", error="boom")

        await scout.advance_scout_job(engine, await db.get_job(job.id))

        assert (await db.get_job(job.id)).status == JobStatus.FAILED
        assert len(engine.spawned) == 1


@pytest.mark.asyncio
class TestReadOnly:
    @pytest.mark.parametrize("tool", ["write_file", "run_command", "commit", "push", "create_pr", "report_pr", "create_trello_tech_debt"])
    async def test_the_executor_refuses_anything_but_reading_and_filing(self, tool, tmp_path):
        executor = McpToolExecutor(mcp_server=None, job_id="j", task_id="t", agent_id="a", agent_role=AgentRole.SCOUT, working_dir=str(tmp_path))

        payload = json.loads(await executor.execute(tool, {"path": "x", "content": "y", "command": "rm -rf ."}))

        assert "may not call" in payload["error"]

    async def test_reading_is_allowed(self, tmp_path):
        (tmp_path / "a.py").write_text("x = 1\n")
        executor = McpToolExecutor(mcp_server=None, job_id="j", task_id="t", agent_id="a", agent_role=AgentRole.SCOUT, working_dir=str(tmp_path))

        out = await executor.execute("read_file", {"path": "a.py"})

        assert "may not call" not in out
        assert "x = 1" in out


@pytest.mark.asyncio
async def test_the_scout_gets_its_own_prompt_and_tools(db, tmp_path):
    from minions.agents.prompt import build_agent_prompt
    from minions.agents.tools.definitions import get_tools_for_role

    engine = _Engine(db, _config(tmp_path), _registry(_service("wallet-api")))
    job = await scout.scout_tick(engine)
    task = (await db.get_tasks(job.id))[0]

    prompt = build_agent_prompt(job, task)

    assert prompt.startswith("# Scout Agent")
    assert {t["function"]["name"] for t in get_tools_for_role("scout", memory_enabled=True)} == {"read_file", "search_code", "submit_scout_finding"}


@pytest.mark.asyncio
async def test_the_engine_routes_scout_jobs_before_the_line_graph(db, tmp_path, monkeypatch):
    """LangGraph is on in production; a scout job must never reach it."""
    from minions.engine.job_engine import JobEngine

    config = _config(tmp_path)
    config.use_langgraph_engine = True
    engine = JobEngine.__new__(JobEngine)
    engine.db = db
    engine.config = config
    routed: list[str] = []

    async def fake_advance(eng, job):
        routed.append(job.id)

    async def graph_must_not_run(eng, job, checkpointer=None):
        raise AssertionError("scout job reached the line's graph")

    monkeypatch.setattr(scout, "advance_scout_job", fake_advance)
    monkeypatch.setattr("minions.engine.job_engine.advance_job_via_graph", graph_must_not_run)
    task_engine = _Engine(db, config, _registry(_service("wallet-api")))
    job = await scout.scout_tick(task_engine)

    await engine._advance(job)

    assert routed == [job.id]
