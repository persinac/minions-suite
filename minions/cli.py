"""CLI entry point for the minion-suite.

Usage:
    minion review <mr-url>                        # One-shot code review
    minion review --watch --project <name>        # Poll for new MRs
    minion job <spec-text-or-file>                # Submit a job and watch
    minion --server                               # MCP server + review engine + job engine + arbiter
    minion --dashboard                            # Web dashboard (read-only job viewer)
    minion --trello-only                          # Trello poller mode
    minion --gitlab-issues-only                   # GitLab issues poller mode
    minion --preflight                            # Health checks
    minion --status                               # Recent reviews
    minion --job-status <JOB_ID>                  # Show job status + tasks
    minion --costs [--project <name>]             # Cost summary
"""

import argparse
import asyncio
import json
import logging
import os
import re
import signal
import sys
import traceback
from pathlib import Path

from .config import Config
from .core.models import Job, JobStatus, TaskStatus
from .db.postgres import PostgresDatabase

logger = logging.getLogger("minions")


def _create_db(config: Config):
    """Create the PostgreSQL database instance."""
    return PostgresDatabase(config.postgres_url, config.postgres_pool_min, config.postgres_pool_max)


def _wire_memory_callbacks(otel_provider, config: Config) -> None:
    """Set up agent-memory trace callbacks (OTEL spans, Prometheus, Postgres persistence)."""
    from .observability.memory_metrics import make_metrics_callback
    from .observability.memory_otel import make_composite_callback, make_otel_callback, make_persistence_callback

    callbacks = []

    metrics_cb = make_metrics_callback()
    if metrics_cb:
        callbacks.append(metrics_cb)

    if otel_provider:
        callbacks.append(make_otel_callback(otel_provider))

    if config.postgres_url:
        callbacks.append(make_persistence_callback(config.postgres_url))

    if callbacks:
        from agent_memory.tracing import set_trace_callback

        set_trace_callback(make_composite_callback(*callbacks))
        logger.info("Memory trace callbacks wired: %d callbacks", len(callbacks))


def _parse_mr_url(url: str) -> tuple[str, str, str]:
    """Extract MR/PR ID, provider hint, and project path from a URL.

    Returns (mr_id, provider_hint, project_path).
    project_path is URL-encoded for GitLab API usage.
    """
    from urllib.parse import quote_plus, urlparse

    parsed = urlparse(url)
    path = parsed.path.strip("/")

    # GitLab: https://gitlab.com/group/repo/-/merge_requests/42
    match = re.search(r"/merge_requests/(\d+)", url)
    if match:
        # Extract project path: everything before /-/merge_requests
        project_path = re.sub(r"/-/merge_requests/\d+.*", "", path)
        return match.group(1), "gitlab", quote_plus(project_path)

    # GitHub: https://github.com/owner/repo/pull/42
    match = re.search(r"/pull/(\d+)", url)
    if match:
        project_path = re.sub(r"/pull/\d+.*", "", path)
        return match.group(1), "github", project_path

    # Fall back to last path segment
    mr_id = url.rstrip("/").split("/")[-1]
    return mr_id, "", ""


def _find_project_for_url(url: str, projects: dict) -> str:
    """Try to match a URL to a project in the registry."""
    for name, project in projects.items():
        if project.project_id and project.project_id.replace("/", "%2F") in url:
            return name
        if project.project_id and project.project_id in url:
            return name
    return ""


async def _run_one_shot(url: str, project_name: str, config: Config) -> int:
    """Run a single review as a Job+Task and exit."""
    from .agents.runner import run_agent
    from .engine.review import _create_provider_for_project
    from .observability.langfuse import create_langfuse_logger
    from .project_registry import build_registry

    # Optional Langfuse tracing
    langfuse_logger, otel_provider = create_langfuse_logger(config)
    if langfuse_logger:
        import litellm

        litellm.callbacks = [langfuse_logger]
        logger.info("Langfuse tracing enabled")

    # Wire memory trace callbacks
    _wire_memory_callbacks(otel_provider, config)

    db = _create_db(config)
    await db.connect()

    try:
        projects = build_registry(config.projects_file)
    except Exception as e:
        logger.error("Failed to load projects: %s", e)
        projects = {}

    # Resolve project
    if not project_name:
        project_name = _find_project_for_url(url, projects)

    if not project_name:
        # Ad-hoc review — create a temporary project config
        from .project_registry import ProjectConfig

        mr_id, provider_hint, project_path = _parse_mr_url(url)
        provider_type = provider_hint or config.git_provider
        project = ProjectConfig(
            name="_adhoc",
            project_id=project_path,
            git_provider=provider_type,
            gitlab_url=config.gitlab_url,
            model=config.model,
        )
        project_name = "_adhoc"
        projects["_adhoc"] = project
    else:
        if project_name not in projects:
            logger.error("Project '%s' not found in projects.yaml", project_name)
            await db.close()
            return 1

    mr_id, _, _ = _parse_mr_url(url)
    project = projects[project_name]

    # Create review job + task
    job, task = await db.create_review_job(project_name, url, mr_id, project.model or config.model)
    print(f"Job {job.id} (task {task.id}) created for {url}")

    # Create git provider
    try:
        provider = _create_provider_for_project(project, config)
    except ValueError as e:
        logger.error("Provider error: %s", e)
        await db.close()
        return 1

    # Fetch MR metadata
    mr_info = {"project_id": project.project_id, "changed_files": []}
    try:
        changed_files = await provider.get_changed_files(project.project_id, mr_id)
        mr_info["changed_files"] = changed_files
    except Exception as e:
        logger.warning("Could not fetch changed files: %s", e)

    # Immediately claim the job so the engine doesn't race us
    fresh_job = await db.get_job(job.id)
    if fresh_job and fresh_job.status == JobStatus.TASKS_CREATED:
        await db.update_job_status(job.id, JobStatus.REVIEW_IN_PROGRESS)
    # Re-fetch task in case the engine already moved it to in_progress
    task = await db.get_task(task.id) or task
    if task.status != TaskStatus.IN_PROGRESS:
        await db.update_task(task.id, status=TaskStatus.IN_PROGRESS)

    agent = await run_agent(
        job=job,
        task=task,
        project=project,
        config=config,
        db=db,
        provider=provider,
        mr_info=mr_info,
    )

    if agent.status == "done":
        verdict = getattr(agent, "_review_verdict", None)
        comments_posted = getattr(agent, "_review_comments_posted", 0)
        await db.update_task(task.id, status=TaskStatus.DONE, verdict=verdict, comments_posted=comments_posted)
        await db.update_job_status(job.id, JobStatus.DONE)

        print(f"\nReview complete: {verdict or 'done'}")
        print(f"  Comments posted: {comments_posted}")
        print(f"  Cost: ${agent.cost_usd:.4f} ({agent.input_tokens + agent.output_tokens} tokens)")
        print(f"  Log: {agent.log_file}")
    else:
        await db.update_task(task.id, status=TaskStatus.FAILED, error=agent.error)
        await db.update_job_status(job.id, JobStatus.FAILED, error=agent.error)
        print(f"\nReview failed: {agent.error}")
        return 1

    await db.close()
    return 0


async def _queue_review(url: str, project_name: str, config: Config) -> int:
    """Create a review job in the DB and exit — the engine picks it up."""
    from .project_registry import build_registry

    db = _create_db(config)
    await db.connect()

    try:
        projects = build_registry(config.projects_file)
    except Exception as e:
        logger.error("Failed to load projects: %s", e)
        projects = {}

    if not project_name:
        project_name = _find_project_for_url(url, projects)

    if not project_name:
        from .project_registry import ProjectConfig

        mr_id, provider_hint, project_path = _parse_mr_url(url)
        provider_type = provider_hint or config.git_provider
        project = ProjectConfig(
            name="_adhoc",
            project_id=project_path,
            git_provider=provider_type,
            gitlab_url=config.gitlab_url,
            model=config.model,
        )
        project_name = "_adhoc"
        projects["_adhoc"] = project
    else:
        if project_name not in projects:
            logger.error("Project '%s' not found in projects.yaml", project_name)
            await db.close()
            return 1

    mr_id, _, _ = _parse_mr_url(url)

    job, task = await db.create_review_job(project_name, url, mr_id, projects[project_name].model or config.model)
    print(f"Queued review job {job.id} (task {task.id}) for {url}")
    print("The engine will pick this up on its next poll cycle.")

    await db.close()
    return 0


async def _run_server(config: Config) -> None:
    """Run the MCP server + job engine + arbiter."""
    from .connectors.nats_client import NatsClient
    from .engine import JobEngine
    from .preflight import print_preflight, run_preflight
    from .project_registry import build_registry
    from .server.mcp import create_server
    from .server.middleware import ToolAuditMiddleware

    # Run preflight checks before anything else
    checks = run_preflight(config)
    ok = print_preflight(checks)
    if not ok:
        logger.error("Preflight checks failed — aborting server startup")
        return

    # Optional Langfuse tracing
    from .observability.langfuse import create_langfuse_logger

    langfuse_logger, otel_provider = create_langfuse_logger(config)
    if langfuse_logger:
        import litellm

        litellm.callbacks = [langfuse_logger]
        logger.info("Langfuse tracing enabled")

    # Wire memory trace callbacks
    _wire_memory_callbacks(otel_provider, config)

    db = _create_db(config)
    await db.connect()

    projects = build_registry(config.projects_file)

    # Optional NATS
    nats_client = None
    if config.nats_enabled:
        from .connectors.nats_config import NatsConfig
        from .connectors.nats_init import ensure_jetstream_stream

        nats_client = NatsClient()
        await nats_client.connect(NatsConfig.from_env())
        await ensure_jetstream_stream(nats_client)

    # Optional K8s launcher
    k8s_launcher = None
    if config.k8s_dispatch:
        from .providers.k8s import K8sJobLauncher

        k8s_launcher = K8sJobLauncher(
            namespace=config.k8s_namespace,
            agent_image=config.k8s_agent_image,
            agent_sa=config.k8s_agent_sa,
            job_ttl=config.k8s_job_ttl,
            secrets_name=config.k8s_secrets_name,
        )

    # Optional memory system (agent-memory)
    memory_tuplespace = None
    memory_store = None
    memory_archiver = None
    if config.memory_enabled:
        # Configure memory trace logging
        import logging as _logging

        mem_log_level = getattr(_logging, config.memory_log_level.upper(), _logging.INFO)
        _logging.getLogger("agent_memory.trace").setLevel(mem_log_level)
        _logging.getLogger("agent_memory").setLevel(mem_log_level)

        try:
            from agent_memory.archiver import MemoryArchiver
            from agent_memory.backends.redis import RedisTupleSpaceBackend
            from agent_memory.store import MemoryStore
            from agent_memory.tuplespace import TupleSpace

            # Create tuplespace (L2 — Redis)
            ts_backend = RedisTupleSpaceBackend(url=config.redis_url, password=config.redis_password or None)
            # Use first project name as default scope; agents override per-job
            first_project = next(iter(projects.keys()), "default")
            memory_tuplespace = TupleSpace(ts_backend, project=first_project)
            await memory_tuplespace.connect()

            # Memory store (L3) reuses the existing Postgres pool if available
            # For now, create a lightweight wrapper — the Postgres backend will be
            # initialised later if POSTGRES_URL is set.
            from agent_memory.backends.postgres import PostgresMemoryBackend

            if config.postgres_url:
                ms_backend = PostgresMemoryBackend(conninfo=config.postgres_url)
                memory_store = MemoryStore(ms_backend)
                await memory_store.connect()

            memory_archiver = MemoryArchiver()
            # Redact: config.redis_url carries the password, and this line goes to
            # stdout -> pod logs -> every aggregator downstream. Same class of leak
            # as the one fixed in preflight.check_redis.
            from .preflight import _redact_url

            logger.info("Memory system enabled (redis=%s)", _redact_url(config.redis_url))
        except Exception:
            logger.exception("Failed to initialise memory system — continuing without it")
            memory_tuplespace = None
            memory_store = None
            memory_archiver = None

    # Create MCP server with audit middleware
    mcp = create_server(db, config, tuplespace=memory_tuplespace, memory_enabled=config.memory_enabled)
    mcp.add_middleware(ToolAuditMiddleware(db))

    # Create artifact uploader
    from .artifact_uploader import ArtifactUploader

    artifact_uploader = ArtifactUploader(db, config)

    # Job engine handles both development and review jobs
    job_engine = JobEngine(
        db,
        config,
        k8s_launcher=k8s_launcher,
        nats_client=nats_client,
        artifact_uploader=artifact_uploader,
        mcp_server=mcp,
        memory_store=memory_store,
        tuplespace=memory_tuplespace,
        archiver=memory_archiver,
    )

    # Optional GitLab issues poller
    gitlab_issues_poller = None
    if config.gitlab_issues_enabled:
        from .providers.gitlab_issues import GitLabIssuesPoller

        gitlab_issues_poller = GitLabIssuesPoller(config, db, projects)

    # Optional arbiter — route MCP tool state mutations through NATS
    arbiter = None
    if config.arbiter_enabled and nats_client:
        from .core.timeout_config import TimeoutConfig
        from .engine.arbiter import Arbiter
        from .server.mcp import set_nats_client

        arbiter = Arbiter(db, TimeoutConfig(), nats_client, engineer_dispatch=config.engineer_dispatch)
        set_nats_client(nats_client)

    # Start all components
    tasks = []
    tasks.append(asyncio.create_task(job_engine.start(), name="job-engine"))
    if gitlab_issues_poller:
        tasks.append(asyncio.create_task(gitlab_issues_poller.start(), name="gitlab-issues-poller"))
    if arbiter:
        tasks.append(asyncio.create_task(arbiter.start(), name="arbiter"))

    # Record WHY the server stops, rather than inferring it afterwards.
    #
    # When mcp.run_async() returns, the `finally` below calls
    # job_engine.stop(), which marks every in-process agent "interrupted by
    # engine shutdown". Agents have been dying that way mid-run with no
    # recorded cause: uvicorn handles the signal gracefully and exits 0, so the
    # pod reports Completed, the ReplicaSet replaces it at restarts=0, and every
    # crash-shaped diagnostic -- OOM, eviction, restart count, node pressure,
    # probes -- comes back clean.
    #
    # Already ruled out: memory (0.47 of 12 GiB at death), OOM, eviction, node
    # pressure, autoscalers, the startup probe (300s budget vs a ~5s bind), a
    # stale rollout-restart annotation, agent shell commands signalling a shared
    # process group (fixed in 070c7db, deaths continued), and running the test
    # command by hand (the pod survived). What remains needs the signal itself.
    _prev_handlers = {}

    def _log_signal(signum, frame):  # pragma: no cover - only fires in-cluster
        name = signal.Signals(signum).name
        logger.error(
            "SERVER RECEIVED %s (pid=%s ppid=%s) -- this stops the MCP server and fails every in-process agent",
            name,
            os.getpid(),
            os.getppid(),
        )
        try:
            logger.error("Stack at %s:\n%s", name, "".join(traceback.format_stack(frame)[-6:]))
        except Exception:
            pass
        previous = _prev_handlers.get(signum)
        if callable(previous):
            previous(signum, frame)
        else:
            raise KeyboardInterrupt(f"{name} received")

    for _sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT):
        try:
            _prev_handlers[_sig] = signal.getsignal(_sig)
            signal.signal(_sig, _log_signal)
        except (ValueError, OSError):
            logger.debug("Could not install handler for %s", _sig)

    try:
        await mcp.run_async(transport="sse", host=config.mcp_host, port=config.mcp_port)
        logger.error("mcp.run_async() RETURNED WITHOUT A SIGNAL -- the server stopped on its own")
    finally:
        logger.warning("Engine shutdown beginning -- in-process agents will be marked failed")
        await job_engine.stop()
        if gitlab_issues_poller:
            await gitlab_issues_poller.stop()
        if arbiter:
            await arbiter.stop()
        for t in tasks:
            t.cancel()
        if memory_tuplespace:
            await memory_tuplespace.close()
        if memory_store:
            await memory_store.close()
        if nats_client:
            await nats_client.close()
        await db.close()


async def _run_trello_only(config: Config) -> None:
    """Run only the Trello poller + job engine (for infrastructure compatibility)."""
    from .engine import JobEngine
    from .providers.trello import TrelloPoller

    if not config.trello_api_key or not config.trello_token or not config.trello_board_id:
        print("Error: TRELLO_API_KEY, TRELLO_TOKEN, and TRELLO_BOARD_ID must be set")
        sys.exit(1)

    db = _create_db(config)
    await db.connect()

    # Optional NATS
    nats_client = None
    if config.nats_enabled:
        from .connectors.nats_client import NatsClient
        from .connectors.nats_config import NatsConfig

        nats_client = NatsClient()
        await nats_client.connect(NatsConfig.from_env())

    # Optional K8s launcher
    k8s_launcher = None
    if config.k8s_dispatch:
        from .providers.k8s import K8sJobLauncher

        k8s_launcher = K8sJobLauncher(
            namespace=config.k8s_namespace,
            agent_image=config.k8s_agent_image,
            agent_sa=config.k8s_agent_sa,
            job_ttl=config.k8s_job_ttl,
            secrets_name=config.k8s_secrets_name,
        )

    from .artifact_uploader import ArtifactUploader
    from .server.mcp import create_server

    artifact_uploader = ArtifactUploader(db, config)
    mcp = create_server(db, config)
    job_engine = JobEngine(db, config, k8s_launcher=k8s_launcher, nats_client=nats_client, artifact_uploader=artifact_uploader, mcp_server=mcp)
    poller = TrelloPoller(config, db)

    # Optional arbiter — route MCP tool state mutations through NATS
    arbiter = None
    if config.arbiter_enabled and nats_client:
        from .core.timeout_config import TimeoutConfig
        from .engine.arbiter import Arbiter
        from .server.mcp import set_nats_client

        arbiter = Arbiter(db, TimeoutConfig(), nats_client, engineer_dispatch=config.engineer_dispatch)
        set_nats_client(nats_client)

    engine_task = asyncio.create_task(job_engine.start(), name="job-engine")
    poller_task = asyncio.create_task(poller.start(), name="trello-poller")
    arbiter_task = None
    if arbiter:
        arbiter_task = asyncio.create_task(arbiter.start(), name="arbiter")

    try:
        # Wait forever (until interrupted)
        await asyncio.Event().wait()
    finally:
        await poller.stop()
        await job_engine.stop()
        if arbiter:
            await arbiter.stop()
        engine_task.cancel()
        poller_task.cancel()
        if arbiter_task:
            arbiter_task.cancel()
        if nats_client:
            await nats_client.close()
        await db.close()


async def _run_gitlab_issues_only(config: Config) -> None:
    """Run only the GitLab issues poller (no job engine — server handles execution)."""
    from .project_registry import build_registry
    from .providers.gitlab_issues import GitLabIssuesPoller

    if not config.gitlab_token:
        print("Error: GITLAB_TOKEN must be set")
        sys.exit(1)

    db = _create_db(config)
    await db.connect()

    projects = build_registry(config.projects_file)
    poller = GitLabIssuesPoller(config, db, projects)

    poller_task = asyncio.create_task(poller.start(), name="gitlab-issues-poller")

    try:
        await asyncio.Event().wait()
    finally:
        await poller.stop()
        poller_task.cancel()
        await db.close()


def _supervise(task: asyncio.Task, shutdown: asyncio.Event) -> asyncio.Task:
    """Make a background task's death visible, and stop the process when it happens.

    Every long-running component here is an asyncio task that nothing ever
    awaits — the parent blocks on an Event instead. An exception inside one is
    therefore never retrieved, and because the task list keeps a strong
    reference it is never garbage collected either, so asyncio's usual
    "Task exception was never retrieved" warning never fires. The failure is
    completely invisible.

    That is not hypothetical: the Trello poller raises from _resolve_list_ids
    when the board is missing a required list, and the process went on logging
    "Input sources started: trello" and reporting 1/1 Running with no poller at
    all. Losing an input source means the container is doing nothing it was
    deployed to do, so the honest response is to log loudly and exit — a
    CrashLoopBackOff is visible where a healthy-looking idle pod is not.
    """

    def _on_done(finished: asyncio.Task) -> None:
        if finished.cancelled():
            return
        exc = finished.exception()
        if exc is None:
            logger.error("Background task %s exited on its own — nothing left to run it", finished.get_name())
        else:
            logger.error("Background task %s died: %s", finished.get_name(), exc, exc_info=exc)
        shutdown.set()

    task.add_done_callback(_on_done)
    return task


async def _run_pollers(config: Config) -> int:
    """Run all configured input source pollers + job engine.

    Starts every poller whose keys/flags are set, skips the rest.
    Designed to run as a standalone container or via `task up`.

    Returns a process exit code: non-zero if a component died.
    """
    from .engine import JobEngine
    from .project_registry import build_registry

    db = _create_db(config)
    await db.connect()

    try:
        projects = build_registry(config.projects_file)
    except Exception as e:
        logger.error("Failed to load projects: %s", e)
        projects = {}

    # Optional NATS
    nats_client = None
    if config.nats_enabled:
        from .connectors.nats_client import NatsClient
        from .connectors.nats_config import NatsConfig

        nats_client = NatsClient()
        await nats_client.connect(NatsConfig.from_env())

    # Optional K8s launcher
    k8s_launcher = None
    if config.k8s_dispatch:
        from .providers.k8s import K8sJobLauncher

        k8s_launcher = K8sJobLauncher(
            namespace=config.k8s_namespace,
            agent_image=config.k8s_agent_image,
            agent_sa=config.k8s_agent_sa,
            job_ttl=config.k8s_job_ttl,
            secrets_name=config.k8s_secrets_name,
        )

    from .artifact_uploader import ArtifactUploader
    from .server.mcp import create_server

    artifact_uploader = ArtifactUploader(db, config)
    mcp = create_server(db, config)
    job_engine = JobEngine(db, config, k8s_launcher=k8s_launcher, nats_client=nats_client, artifact_uploader=artifact_uploader, mcp_server=mcp)

    tasks = []
    sources_started = []
    shutdown = asyncio.Event()

    # Job engine — only when this process owns it. Running a second engine
    # alongside `--server` double-dispatches: both poll the same jobs table and
    # each launches its own agents for the same job (launch_spec_analyst's only
    # guard is a _has_running_agent read, which both pass). Pollers write jobs to
    # the DB and need no engine of their own.
    if config.engine_enabled:
        tasks.append(_supervise(asyncio.create_task(job_engine.start(), name="job-engine"), shutdown))
    else:
        logger.info("Job engine disabled (ENGINE_ENABLED=false) — this process only feeds jobs to the DB")

    # GitLab Issues
    if config.gitlab_issues_enabled and config.gitlab_token:
        from .providers.gitlab_issues import GitLabIssuesPoller

        gitlab_poller = GitLabIssuesPoller(config, db, projects)
        tasks.append(_supervise(asyncio.create_task(gitlab_poller.start(), name="gitlab-issues-poller"), shutdown))
        sources_started.append("gitlab-issues")
    else:
        gitlab_poller = None

    # Trello
    if config.trello_api_key and config.trello_token and config.trello_board_id:
        from .providers.trello import TrelloPoller

        trello_poller = TrelloPoller(config, db)
        tasks.append(_supervise(asyncio.create_task(trello_poller.start(), name="trello-poller"), shutdown))
        sources_started.append("trello")
    else:
        trello_poller = None

    # Renovate
    if config.renovate_enabled:
        from .renovate.engine import RenovateEngine

        renovate_engine = RenovateEngine(db, config, projects, nats_client=nats_client)
        tasks.append(_supervise(asyncio.create_task(renovate_engine.start(), name="renovate-engine"), shutdown))
        sources_started.append("renovate")
    else:
        renovate_engine = None

    if sources_started:
        logger.info("Input sources started: %s", ", ".join(sources_started))
    elif config.engine_enabled:
        logger.warning("No input sources configured — job engine running but no pollers active")
    else:
        logger.warning("No input sources configured and engine disabled — this process will do nothing")

    try:
        # Wakes on a component death as well as on signal, so a dead poller ends
        # the process instead of leaving it idle and reporting 1/1 Running.
        await shutdown.wait()
        logger.error("A component died — shutting down so the failure is visible to the orchestrator")
        return 1
    finally:
        # Only stop the engine if we started it. JobEngine.stop() marks every
        # in-process agent in the database as failed, so calling it from a
        # poller-only process would kill the agents belonging to whichever
        # process actually owns the engine — on any routine rollout or restart.
        if config.engine_enabled:
            await job_engine.stop()
        if gitlab_poller:
            await gitlab_poller.stop()
        if trello_poller:
            await trello_poller.stop()
        if renovate_engine:
            await renovate_engine.stop()
        for t in tasks:
            t.cancel()
        if nats_client:
            await nats_client.close()
        await db.close()


async def _run_job(spec_text: str, config: Config) -> int:
    """Submit a job and run the engine until it completes."""
    from .core.models import JobStatus
    from .engine import JobEngine

    db = _create_db(config)
    await db.connect()

    # create_job takes the spec string and builds the Job itself; passing a
    # pre-built Job fails pydantic validation. Third instance of the same bug —
    # submit_spec and the Trello poller had it too.
    job = await db.create_job(spec_text)
    print(f"Job {job.id} created (status: {job.status})")

    # Optional NATS
    nats_client = None
    if config.nats_enabled:
        from .connectors.nats_client import NatsClient
        from .connectors.nats_config import NatsConfig

        nats_client = NatsClient()
        await nats_client.connect(NatsConfig.from_env())

    from .artifact_uploader import ArtifactUploader
    from .server.mcp import create_server

    artifact_uploader = ArtifactUploader(db, config)
    mcp = create_server(db, config)
    engine = JobEngine(db, config, nats_client=nats_client, artifact_uploader=artifact_uploader, mcp_server=mcp)

    terminal_statuses = {JobStatus.DONE, JobStatus.FAILED, JobStatus.NO_WORK_NEEDED}
    exit_code = 0

    # Run engine in background, poll job status
    engine_task = asyncio.create_task(engine.start(), name="job-engine")

    try:
        last_status = None
        while True:
            await asyncio.sleep(2)
            current_job = await db.get_job(job.id)
            if not current_job:
                print("Error: job disappeared from DB")
                exit_code = 1
                break
            if current_job.status != last_status:
                last_status = current_job.status
                print(f"  Status: {current_job.status}")
            if current_job.status in terminal_statuses:
                if current_job.status == JobStatus.FAILED:
                    print(f"\nJob failed: {current_job.error or 'unknown'}")
                    exit_code = 1
                elif current_job.status == JobStatus.NO_WORK_NEEDED:
                    print("\nJob completed: no work needed")
                else:
                    print("\nJob completed successfully!")
                break
    finally:
        await engine.stop()
        engine_task.cancel()
        if nats_client:
            await nats_client.close()
        await db.close()

    return exit_code


async def _show_status(config: Config) -> None:
    """Show recent review job status."""
    db = _create_db(config)
    await db.connect()

    all_jobs = await db.get_all_jobs()
    review_jobs = [j for j in all_jobs if j.job_type == "review"][:20]
    if not review_jobs:
        print("No review jobs found.")
        await db.close()
        return

    print(f"\n{'Job ID':<10} {'Status':<20} {'MR URL':<50} {'Created'}")
    print("-" * 110)
    for j in review_jobs:
        tasks = await db.get_tasks(j.id)
        # Show task-level detail
        for t in tasks:
            verdict_str = t.verdict or "-"
            print(f"{j.id:<10} {j.status:<20} {(t.mr_url or j.mr_url or '-')[:50]:<50} {j.created_at[:19]}")
            if t.verdict or t.comments_posted:
                print(f"{'':10} verdict={verdict_str} comments={t.comments_posted}")

    await db.close()


async def _show_job_status(config: Config, job_id: str) -> None:
    """Show job status with tasks and agents."""
    db = _create_db(config)
    await db.connect()

    job = await db.get_job(job_id)
    if not job:
        print(f"Job {job_id} not found.")
        await db.close()
        return

    print(f"\nJob {job.id}")
    print(f"  Status:     {job.status}")
    print(f"  Created:    {job.created_at}")
    print(f"  Updated:    {job.updated_at}")
    if job.error:
        print(f"  Error:      {job.error}")
    print(f"  Spec:       {job.spec[:100]}{'...' if len(job.spec) > 100 else ''}")

    tasks = await db.get_tasks(job_id)
    if tasks:
        print(f"\n  Tasks ({len(tasks)}):")
        for t in tasks:
            status_str = str(t.status)
            pr_str = f" PR={t.pr_url}" if t.pr_url else ""
            error_str = f" ERROR={t.error[:50]}" if t.error else ""
            print(f"    {t.id[:8]} [{status_str:<12}] {t.agent_role:<20} {t.service:<15} {t.title[:40]}{pr_str}{error_str}")

    agents = await db.get_agents_for_job(job_id)
    if agents:
        print(f"\n  Agents ({len(agents)}):")
        for a in agents:
            cost_str = f"${a.cost_usd:.4f}" if a.cost_usd else "$0"
            print(f"    {a.id[:8]} [{a.status:<10}] {a.role or '-':<20} {a.model:<20} {cost_str}")

    await db.close()


async def _show_costs(config: Config, project: str | None = None) -> None:
    """Show cost summary."""
    db = _create_db(config)
    await db.connect()

    summary = await db.get_cost_summary(project=project)
    print(f"\n=== Cost Summary (last {summary['period_days']} days) ===")
    if project:
        print(f"Project: {project}")
    print(f"  Total reviews: {summary['total_reviews']}")
    print(f"  Total cost:    ${summary['total_cost_usd']:.4f}")
    print(f"  Avg per review: ${summary['avg_cost_per_review']:.4f}")
    print(f"  Total tokens:  {summary['total_input_tokens'] + summary['total_output_tokens']:,}")

    await db.close()


async def _run_agent_worker(config: Config) -> int:
    """K8s agent worker mode: pull work item, run LiteLLM loop, publish result."""
    import os

    from .agents.dispatch import AgentResultMessage, deserialize_work_item

    work_item_path = os.getenv("AGENT_WORK_ITEM_PATH")
    if not work_item_path:
        print("Error: AGENT_WORK_ITEM_PATH not set")
        return 1

    work_item_data = Path(work_item_path).read_bytes()
    work_item = deserialize_work_item(work_item_data)

    logger.info("Agent worker starting: role=%s job=%s agent=%s", work_item.role, work_item.job_id, work_item.agent_id)

    # Build a minimal Job and Task for the agent loop
    from .core.models import Task

    job = Job(id=work_item.job_id, spec="(loaded from work item)")
    task = Task(
        job_id=work_item.job_id,
        title="Worker task",
        description=work_item.prompt,
        service="_worker",
        agent_role=work_item.role,
    )

    from .agents.runner import run_agent

    agent = await run_agent(job=job, task=task, config=config)

    # Build result message
    result_msg = AgentResultMessage(
        job_id=work_item.job_id,
        agent_id=work_item.agent_id,
        role=work_item.role,
        success=(agent.status == "done"),
        return_code=0 if agent.status == "done" else 1,
        log_file=agent.log_file or "",
        stderr_tail=agent.error or "",
        input_tokens=agent.input_tokens,
        output_tokens=agent.output_tokens,
        cache_read_tokens=agent.cache_read_tokens,
        cache_creation_tokens=agent.cache_creation_tokens,
        cost_usd=agent.cost_usd,
        num_turns=agent.num_turns,
        model=agent.model,
    )

    # Publish result via NATS if available
    if config.nats_enabled:
        try:
            from .connectors.nats_publisher import publish_agent_status

            await publish_agent_status(work_item.job_id, work_item.agent_id, work_item.role, agent.status)
        except Exception:
            logger.debug("Failed to publish NATS result", exc_info=True)

    # Write result to stdout for K8s log collection
    print(
        json.dumps(
            {
                "agent_id": result_msg.agent_id,
                "success": result_msg.success,
                "cost_usd": result_msg.cost_usd,
                "num_turns": result_msg.num_turns,
            }
        )
    )

    return 0 if result_msg.success else 1


def main():
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="minion",
        description="Minion Suite — AI agent suite for code review and multi-agent job orchestration",
    )
    subparsers = parser.add_subparsers(dest="command")

    # minion review <url>
    review_parser = subparsers.add_parser("review", help="Review a merge/pull request")
    review_parser.add_argument("mr_url", help="MR/PR URL to review")
    review_parser.add_argument("--project", "-p", help="Project name from projects.yaml")
    review_parser.add_argument("--async", dest="async_mode", action="store_true", help="Queue for engine pickup instead of running inline")

    # minion job <spec>
    job_parser = subparsers.add_parser("job", help="Submit a job specification")
    job_parser.add_argument("spec", help="Specification text or path to .md file")

    # minion agent-worker (K8s mode)
    subparsers.add_parser("agent-worker", help="Run as K8s agent worker (reads from AGENT_WORK_ITEM_PATH)")

    # Global flags
    parser.add_argument("--server", action="store_true", help="Run MCP server + review engine + job engine + arbiter")
    parser.add_argument("--dashboard", action="store_true", help="Run web dashboard (read-only job viewer)")
    parser.add_argument("--pollers", action="store_true", help="Run all configured input source pollers + job engine")
    parser.add_argument("--trello-only", action="store_true", help="Run Trello poller + job engine only")
    parser.add_argument("--gitlab-issues-only", action="store_true", help="Run GitLab issues poller + job engine only")
    parser.add_argument("--preflight", action="store_true", help="Run health checks")
    parser.add_argument("--status", action="store_true", help="Show recent review status")
    parser.add_argument("--job-status", metavar="JOB_ID", help="Show job status + tasks + agents")
    parser.add_argument("--costs", action="store_true", help="Show cost summary")
    parser.add_argument("--backfill-artifacts", action="store_true", help="Upload artifacts for completed jobs to S3")
    parser.add_argument("--project", "-p", dest="global_project", help="Project filter (for --costs)")

    args = parser.parse_args()

    config = Config.from_env()

    # Configure logging
    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(name)-20s %(levelname)-5s %(message)s",
        datefmt="%H:%M:%S",
    )

    # Scrub credentials from every record before it can reach a log, a terminal
    # or an agent transcript. Two credentials had to be rotated in one afternoon
    # — one from a call site that logged a Redis URL with the password inline —
    # and fixing call sites one at a time only fixes the ones already known.
    from .redaction import install as install_redaction

    install_redaction()

    if args.preflight:
        from .preflight import print_preflight, run_preflight

        checks = run_preflight(config)
        ok = print_preflight(checks)
        sys.exit(0 if ok else 1)

    if args.status:
        asyncio.run(_show_status(config))
        return

    if args.job_status:
        asyncio.run(_show_job_status(config, args.job_status))
        return

    if args.costs:
        asyncio.run(_show_costs(config, args.global_project))
        return

    if args.backfill_artifacts:
        asyncio.run(_backfill_artifacts(config))
        return

    if args.server:
        asyncio.run(_run_server(config))
        return

    if args.dashboard:
        from .dashboard import run_dashboard

        run_dashboard()
        return

    if args.pollers:
        # Non-zero when a component died, so the container restarts instead of
        # sitting there Running with nothing actually polling.
        sys.exit(asyncio.run(_run_pollers(config)))

    if args.trello_only:
        asyncio.run(_run_trello_only(config))
        return

    if args.gitlab_issues_only:
        asyncio.run(_run_gitlab_issues_only(config))
        return

    if args.command == "review":
        if args.async_mode:
            exit_code = asyncio.run(_queue_review(args.mr_url, args.project, config))
        else:
            exit_code = asyncio.run(_run_one_shot(args.mr_url, args.project, config))
        sys.exit(exit_code)
        return

    if args.command == "job":
        spec = args.spec
        # Check if it's a file path
        spec_path = Path(spec)
        if spec_path.exists() and spec_path.is_file():
            spec = spec_path.read_text(encoding="utf-8")
        exit_code = asyncio.run(_run_job(spec, config))
        sys.exit(exit_code)
        return

    if args.command == "agent-worker":
        exit_code = asyncio.run(_run_agent_worker(config))
        sys.exit(exit_code)
        return

    parser.print_help()
    sys.exit(1)


async def _backfill_artifacts(config: Config):
    """Upload artifacts for all completed jobs to S3."""
    from .artifact_uploader import ArtifactUploader

    db = _create_db(config)
    await db.connect()

    uploader = ArtifactUploader(db, config)
    if not uploader.is_enabled():
        print("S3 artifact upload not configured. Set S3_ARTIFACT_BUCKET to enable.")
        return

    print(f"Backfilling artifacts to s3://{config.s3_artifact_bucket}/{config.s3_artifact_prefix} ...")
    await uploader.backfill()
    print("Backfill complete.")

    await db.close()


if __name__ == "__main__":
    main()
