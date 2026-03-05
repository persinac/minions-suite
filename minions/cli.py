"""CLI entry point for the minion-suite.

Usage:
    minion review <mr-url>                        # One-shot code review
    minion review --watch --project <name>        # Poll for new MRs
    minion job <spec-text-or-file>                # Submit a job and watch
    minion --server                               # MCP server + review engine + job engine + arbiter
    minion --dashboard                            # Web dashboard (read-only job viewer)
    minion --trello-only                          # Trello poller mode
    minion --preflight                            # Health checks
    minion --status                               # Recent reviews
    minion --job-status <JOB_ID>                  # Show job status + tasks
    minion --costs [--project <name>]             # Cost summary
"""

import argparse
import asyncio
import json
import logging
import re
import signal
import sys
from pathlib import Path

from .config import Config
from .core.models import AgentRole, Job, JobStatus, TaskStatus
from .db import SQLiteDatabase

logger = logging.getLogger("minions")


def _create_db(config: Config):
    """Create the appropriate database instance."""
    if config.db_backend == "postgres":
        from .db.postgres import PostgresDatabase

        return PostgresDatabase(config.postgres_url, config.postgres_pool_min, config.postgres_pool_max)
    return SQLiteDatabase(config.db_path)


def _parse_mr_url(url: str) -> tuple[str, str]:
    """Extract MR/PR ID from a URL.

    Returns (mr_id, provider_hint).
    """
    # GitLab: .../merge_requests/42
    match = re.search(r"/merge_requests/(\d+)", url)
    if match:
        return match.group(1), "gitlab"

    # GitHub: .../pull/42
    match = re.search(r"/pull/(\d+)", url)
    if match:
        return match.group(1), "github"

    # Fall back to last path segment
    mr_id = url.rstrip("/").split("/")[-1]
    return mr_id, ""


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
    from .agent import run_agent
    from .git_provider import create_provider
    from .job_engine import _create_provider_for_project
    from .project_registry import build_registry

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

        mr_id, provider_hint = _parse_mr_url(url)
        provider_type = provider_hint or config.git_provider
        project = ProjectConfig(
            name="_adhoc",
            project_id="",
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

    mr_id, _ = _parse_mr_url(url)
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

    # Mark task in-progress and run directly
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
        await db.update_job_status(job.id, JobStatus.REVIEW_IN_PROGRESS)
        await db.update_job_status(job.id, JobStatus.DONE)

        print(f"\nReview complete: {verdict or 'done'}")
        print(f"  Comments posted: {comments_posted}")
        print(f"  Cost: ${agent.cost_usd:.4f} ({agent.input_tokens + agent.output_tokens} tokens)")
        print(f"  Log: {agent.log_file}")
    else:
        await db.update_task(task.id, status=TaskStatus.FAILED, error=agent.error)
        await db.update_job_status(job.id, JobStatus.REVIEW_IN_PROGRESS)
        await db.update_job_status(job.id, JobStatus.FAILED, error=agent.error)
        print(f"\nReview failed: {agent.error}")
        return 1

    await db.close()
    return 0


async def _run_server(config: Config) -> None:
    """Run the MCP server + job engine + arbiter."""
    from .connectors.nats_client import NatsClient
    from .job_engine import JobEngine
    from .preflight import print_preflight, run_preflight
    from .project_registry import build_registry
    from .server import create_server
    from .tool_audit_middleware import ToolAuditMiddleware

    # Run preflight checks before anything else
    checks = run_preflight(config)
    ok = print_preflight(checks)
    if not ok:
        logger.error("Preflight checks failed — aborting server startup")
        return

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
        from .k8s_launcher import K8sJobLauncher

        k8s_launcher = K8sJobLauncher(
            namespace=config.k8s_namespace,
            agent_image=config.k8s_agent_image,
            agent_sa=config.k8s_agent_sa,
            job_ttl=config.k8s_job_ttl,
            secrets_name=config.k8s_secrets_name,
        )

    # Create MCP server with audit middleware
    mcp = create_server(db, config)
    mcp.add_middleware(ToolAuditMiddleware(db))

    # Create artifact uploader
    from .artifact_uploader import ArtifactUploader

    artifact_uploader = ArtifactUploader(db, config)

    # Job engine handles both development and review jobs
    job_engine = JobEngine(db, config, k8s_launcher=k8s_launcher, nats_client=nats_client, artifact_uploader=artifact_uploader, mcp_server=mcp)

    # Optional GitLab issues poller
    gitlab_issues_poller = None
    if config.gitlab_issues_enabled:
        from .gitlab_issues_poller import GitLabIssuesPoller

        gitlab_issues_poller = GitLabIssuesPoller(config, db, projects)

    # Optional arbiter — route MCP tool state mutations through NATS
    arbiter = None
    if config.arbiter_enabled and nats_client:
        from .arbiter import Arbiter
        from .core.timeout_config import TimeoutConfig
        from .server import set_nats_client

        arbiter = Arbiter(db, TimeoutConfig(), nats_client)
        set_nats_client(nats_client)

    # Start all components
    tasks = []
    tasks.append(asyncio.create_task(job_engine.start(), name="job-engine"))
    if gitlab_issues_poller:
        tasks.append(asyncio.create_task(gitlab_issues_poller.start(), name="gitlab-issues-poller"))
    if arbiter:
        tasks.append(asyncio.create_task(arbiter.start(), name="arbiter"))

    try:
        await mcp.run_async(transport="sse", host=config.mcp_host, port=config.mcp_port)
    finally:
        await job_engine.stop()
        if gitlab_issues_poller:
            await gitlab_issues_poller.stop()
        if arbiter:
            await arbiter.stop()
        for t in tasks:
            t.cancel()
        if nats_client:
            await nats_client.close()
        await db.close()


async def _run_trello_only(config: Config) -> None:
    """Run only the Trello poller + job engine (for infrastructure compatibility)."""
    from .job_engine import JobEngine
    from .trello_poller import TrelloPoller

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
        from .k8s_launcher import K8sJobLauncher

        k8s_launcher = K8sJobLauncher(
            namespace=config.k8s_namespace,
            agent_image=config.k8s_agent_image,
            agent_sa=config.k8s_agent_sa,
            job_ttl=config.k8s_job_ttl,
            secrets_name=config.k8s_secrets_name,
        )

    from .artifact_uploader import ArtifactUploader
    from .server import create_server

    artifact_uploader = ArtifactUploader(db, config)
    mcp = create_server(db, config)
    job_engine = JobEngine(db, config, k8s_launcher=k8s_launcher, nats_client=nats_client, artifact_uploader=artifact_uploader, mcp_server=mcp)
    poller = TrelloPoller(config, db)

    # Optional arbiter — route MCP tool state mutations through NATS
    arbiter = None
    if config.arbiter_enabled and nats_client:
        from .arbiter import Arbiter
        from .core.timeout_config import TimeoutConfig
        from .server import set_nats_client

        arbiter = Arbiter(db, TimeoutConfig(), nats_client)
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


async def _run_job(spec_text: str, config: Config) -> int:
    """Submit a job and run the engine until it completes."""
    from .core.models import JobStatus
    from .job_engine import JobEngine

    db = _create_db(config)
    await db.connect()

    # Create job
    job = Job(spec=spec_text)
    job = await db.create_job(job)
    print(f"Job {job.id} created (status: {job.status})")

    # Optional NATS
    nats_client = None
    if config.nats_enabled:
        from .connectors.nats_client import NatsClient
        from .connectors.nats_config import NatsConfig

        nats_client = NatsClient()
        await nats_client.connect(NatsConfig.from_env())

    from .artifact_uploader import ArtifactUploader
    from .server import create_server

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


async def _show_costs(config: Config, project: str = None) -> None:
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

    from .agent_dispatch import AgentResultMessage, deserialize_work_item, serialize_result

    work_item_path = os.getenv("AGENT_WORK_ITEM_PATH")
    if not work_item_path:
        print("Error: AGENT_WORK_ITEM_PATH not set")
        return 1

    work_item_data = Path(work_item_path).read_bytes()
    work_item = deserialize_work_item(work_item_data)

    logger.info("Agent worker starting: role=%s job=%s agent=%s", work_item.role, work_item.job_id, work_item.agent_id)

    working_dir = os.getenv("AGENT_WORKING_DIR", work_item.working_dir)

    # Build a minimal Job and Task for the agent loop
    from .core.models import AgentRole, Task

    job = Job(id=work_item.job_id, spec="(loaded from work item)")
    task = Task(
        job_id=work_item.job_id,
        title="Worker task",
        description=work_item.prompt,
        service="_worker",
        agent_role=work_item.role,
    )

    from .agent import run_agent

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
    print(json.dumps({
        "agent_id": result_msg.agent_id,
        "success": result_msg.success,
        "cost_usd": result_msg.cost_usd,
        "num_turns": result_msg.num_turns,
    }))

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

    # minion job <spec>
    job_parser = subparsers.add_parser("job", help="Submit a job specification")
    job_parser.add_argument("spec", help="Specification text or path to .md file")

    # minion agent-worker (K8s mode)
    subparsers.add_parser("agent-worker", help="Run as K8s agent worker (reads from AGENT_WORK_ITEM_PATH)")

    # Global flags
    parser.add_argument("--server", action="store_true", help="Run MCP server + review engine + job engine + arbiter")
    parser.add_argument("--dashboard", action="store_true", help="Run web dashboard (read-only job viewer)")
    parser.add_argument("--trello-only", action="store_true", help="Run Trello poller + job engine only")
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

    if args.trello_only:
        asyncio.run(_run_trello_only(config))
        return

    if args.command == "review":
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
