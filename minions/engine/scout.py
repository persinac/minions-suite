"""The scout station: find work worth doing, and file it as cards.

openspec/changes/factory-stations/design.md, "Scout". One run looks at one repo:

1. **Schedule** (`scout_tick`) -- at most one scout job at a time, spaced so the
   day's `max_runs_per_day` spread across it, admitted only if the station
   budget allows. The repo is the allowlisted one scouted longest ago.
2. **Signals** (`advance_scout_job`, TASKS_CREATED) -- deterministic, no model:
   git churn x size, TODO density, missing test command (engine/scout_signals.py).
   If they point nowhere, the run ends here having spent nothing.
3. **Findings** (TASKS_CREATED -> SCOUTING) -- one pinned-model agent reads the
   code at those places and files at most `scout_max_findings` findings through
   `submit_scout_finding`, which enforces the finding contract.
4. **Close** (SCOUTING) -- the job ends when its agent does.

What the scout never does: edit code, open a PR, or queue work. Findings land in
the `Inbox` lane; putting a card in front of the line is the gate's decision --
the groomer today, the weight station once it exists. That holds even with
scout_autoqueue agreed on (Alex, 2026-10-03), because autoqueue is defined as
"weight-eligible scout cards go to On-deck", and there is no weight yet.
"""

import logging
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from ..core.models import Agent, AgentRole, Job, JobStatus, Task, TaskStatus
from ..core.stations import BUDGET_WINDOW, SCOUT, station_budget, station_model
from ..repos import ensure_checkout
from .scout_signals import collect_signals, render_signals, worth_a_model_call

logger = logging.getLogger(__name__)


def scout_checkout_root(config) -> Path:
    """Where the scout keeps its own clones -- never an engineer's checkout."""
    if config.scout_checkout_dir:
        return Path(config.scout_checkout_dir)
    return Path(config.repo_base_dir) / ".scout"


def _excluded(config, project_name: str, service_name: str) -> bool:
    excluded = {r.strip().lower() for r in config.scout_exclude_repos}
    return project_name.lower() in excluded or service_name.lower() in excluded


def eligible_services(registry: dict, config) -> list[tuple[str, str, object]]:
    """(project, service_name, service) for every repo the scout may visit."""
    out = []
    for project_name, project in registry.items():
        for service_name, service in (project.services or {}).items():
            if not service.clone_url:
                continue
            if _excluded(config, project_name, service_name):
                continue
            out.append((project_name, service_name, service))
    return out


def pick_next(candidates: list[tuple[str, str, object]], last_run: dict[str, str]) -> tuple[str, str, object] | None:
    """Oldest-scouted first; never-scouted before anything; ties by name.

    Round-robin falls out of this: every run moves its repo to the back.
    """
    if not candidates:
        return None
    return min(candidates, key=lambda c: (last_run.get(c[1], ""), c[1]))


async def scout_tick(engine, now: datetime | None = None) -> Job | None:
    """Schedule one scout run if one is due and allowed. Returns the job, or None."""
    config = engine.config
    db = engine.db
    if not config.scout_enabled:
        return None
    if now is None:
        now = datetime.now(UTC)

    if not await db.scout_tables_exist():
        if not getattr(engine, "_scout_missing_tables_logged", False):
            logger.error(
                "Scout is enabled but scout_signals/scout_findings do not exist — apply "
                "database/pgsql/migrations/20261003120000_add_scout_tables.sql. Scout stays off until then."
            )
            engine._scout_missing_tables_logged = True
        return None

    active = [j for j in await db.get_active_jobs() if j.job_type == SCOUT]
    if active:
        return None

    # Spread the day's runs across the day rather than spending them all at
    # once: with 3 runs a day, one every 8 hours.
    budget = station_budget(config, SCOUT)
    if budget.max_runs_per_day > 0:
        last = await db.get_last_station_job_at(SCOUT)
        if last is not None:
            spacing = BUDGET_WINDOW / budget.max_runs_per_day
            if now - datetime.fromisoformat(last) < spacing:
                return None

    decision = await engine.station_guard.check(db, config, SCOUT, now=now)
    if not decision.allowed:
        return None

    target = pick_next(eligible_services(engine.registry, config), await db.get_last_run_per_service(SCOUT))
    if target is None:
        return None
    project_name, service_name, _ = target

    task = Task(
        job_id="pending",
        title=f"Scout {service_name}",
        description=f"Find up to {config.scout_max_findings} piece(s) of work worth doing in {service_name}, and file each as a card.",
        service=service_name,
        agent_role=AgentRole.SCOUT,
        max_attempts=1,
    )
    job, _ = await db.create_station_job(SCOUT, f"Scout run on {service_name} (project {project_name})", task)
    await db.record_event(job.id, "scout_scheduled", "engine", f"repo={service_name} runs_24h={decision.runs} spend_24h=${decision.spend_usd:.2f}")
    logger.info("Scout scheduled job %s on %s", job.id, service_name)
    return job


async def advance_scout_job(engine, job: Job) -> None:
    """Drive a scout job one step. Called from the engine's poll loop."""
    tasks = await engine.db.get_tasks(job.id)
    task = next((t for t in tasks if t.agent_role == AgentRole.SCOUT), None)
    if task is None:
        await _fail(engine, job, None, "scout job has no scout task")
        return

    if job.status == JobStatus.TASKS_CREATED:
        await _start(engine, job, task)
    elif job.status == JobStatus.SCOUTING:
        await _check(engine, job, task)


async def _start(engine, job: Job, task: Task) -> None:
    """Signals, then (if they point anywhere) launch the findings agent."""
    db = engine.db
    config = engine.config
    project, service = engine._resolve_service(task.service)
    if service is None or not service.clone_url:
        await _fail(engine, job, task, f"service {task.service!r} has no clone_url in projects.yaml")
        return

    repo_dir = scout_checkout_root(config) / task.service
    repo_dir.parent.mkdir(parents=True, exist_ok=True)
    # reset_dirty=True is safe here and only here: this tree belongs to the
    # scout alone, and the scout never writes to it.
    if not await ensure_checkout(service.clone_url, str(repo_dir), service.default_branch, reset_dirty=True):
        await _fail(engine, job, task, f"could not check out {task.service} into {repo_dir}")
        return

    signals = await collect_signals(str(repo_dir), task.service, service)
    await db.record_scout_signals(job.id, task.service, signals)

    await db.update_task(task.id, status=TaskStatus.IN_PROGRESS, agent_role="")
    await db.update_job_status(job.id, JobStatus.SCOUTING)

    if not worth_a_model_call(signals):
        await db.record_event(job.id, "scout_nothing_to_report", "engine", f"repo={task.service} — signals point nowhere, no model call")
        await db.update_task(task.id, status=TaskStatus.DONE, agent_role="")
        await db.update_job_status(job.id, JobStatus.DONE)
        return

    # Second budget check, at the point the money would actually be spent. The
    # run cap was applied when this run was admitted; spend may have moved.
    decision = await engine.station_guard.check(db, config, SCOUT, include_runs=False)
    if not decision.allowed:
        await _fail(engine, job, task, f"station budget exhausted before launch: {decision.reason}")
        return
    remaining = station_budget(config, SCOUT).daily_usd - decision.spend_usd

    agent = await db.create_agent(Agent(job_id=job.id, role=AgentRole.SCOUT, task_id=task.id, model=station_model(config, SCOUT)))
    scout_service = replace(service, repo_path=str(repo_dir))
    engine._spawn(
        engine._run_in_process(
            job,
            task,
            agent,
            project,
            scout_service,
            context=render_signals(signals, config.scout_max_findings),
            cost_limit_usd=max(remaining, 0.0),
        ),
        name=f"scout-{job.id}",
    )
    await db.record_event(job.id, "scout_launched", "engine", f"agent={agent.id} repo={task.service} cost_cap=${remaining:.2f}")


async def _check(engine, job: Job, task: Task) -> None:
    """Close the job once its agent has finished, one way or the other."""
    agent = await engine.db.get_agent_for_task(task.id)
    if agent is not None and agent.status in ("starting", "running"):
        return
    if agent is not None and agent.status == "done":
        filed = await engine.db.count_filed_scout_findings(job.id)
        await engine.db.update_task(task.id, status=TaskStatus.DONE, agent_role="")
        await engine.db.update_job_status(job.id, JobStatus.DONE)
        await engine.db.record_event(job.id, "scout_completed", "engine", f"repo={task.service} filed={filed}")
        return
    # Failed, or gone (a restart orphans it). A scout run is not retried: the
    # repo goes back in the round-robin and is picked up on a later run.
    error = "scout agent ended without finishing"
    if agent is not None and agent.error:
        error = f"scout agent failed: {agent.error[:200]}"
    await _fail(engine, job, task, error)


async def _fail(engine, job: Job, task: Task | None, error: str) -> None:
    logger.warning("Scout job %s failed: %s", job.id, error)
    if task is not None and task.status not in (TaskStatus.DONE, TaskStatus.FAILED):
        await engine.db.update_task(task.id, status=TaskStatus.FAILED, agent_role="", error=error[:500])
    await engine.db.update_job_status(job.id, JobStatus.FAILED, error=error[:500])
    await engine.db.record_event(job.id, "scout_failed", "engine", error[:500])
