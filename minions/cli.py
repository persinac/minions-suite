"""CLI entry point for the minion-suite.

Usage:
    minion review <mr-url>                        # One-shot code review
    minion review --watch --project <name>        # Poll for new MRs
    minion --server                               # MCP server + review engine
    minion --preflight                            # Health checks
    minion --status                               # Recent reviews
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
from .db import SQLiteDatabase
from .models import Review, ReviewStatus


logger = logging.getLogger("minions")


def _create_db(config: Config):
    """Create the appropriate database instance."""
    if config.db_backend == "postgres":
        from .db_postgres import PostgresDatabase

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
    """Run a single review and exit."""
    from .project_registry import build_registry

    db = _create_db(config)
    await db.connect()

    try:
        projects = build_registry(config.projects_file)
    except Exception as e:
        logger.error("Failed to load projects: %s", e)
        # Create a minimal project config for ad-hoc reviews
        projects = {}

    # Resolve project
    if not project_name:
        project_name = _find_project_for_url(url, projects)

    if not project_name:
        # Ad-hoc review — create a temporary project config
        from .project_registry import ProjectConfig

        mr_id, provider_hint = _parse_mr_url(url)
        provider = provider_hint or config.git_provider
        project = ProjectConfig(
            name="_adhoc",
            project_id="",
            git_provider=provider,
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

    # Create review
    review = Review(project=project_name, mr_url=url, mr_id=mr_id, model=project.model or config.model)
    review = await db.create_review(review)
    print(f"Review {review.id} queued for {url}")

    # Run directly (no engine needed for one-shot)
    from .agent import run_review
    from .git_provider import create_provider
    from .review_engine import _create_provider_for_project

    try:
        provider = _create_provider_for_project(project, config)
    except ValueError as e:
        logger.error("Provider error: %s", e)
        await db.close()
        return 1

    await db.update_review(review.id, status=ReviewStatus.IN_PROGRESS)
    agent = await run_review(review, project, provider, config, db)

    if agent.status == "done":
        await db.update_review(
            review.id,
            status=ReviewStatus.DONE,
            verdict=review.verdict,
            summary=review.summary,
            comments_posted=review.comments_posted,
        )
        print(f"\nReview complete: {review.verdict or 'done'}")
        print(f"  Comments posted: {review.comments_posted}")
        print(f"  Cost: ${agent.cost_usd:.4f} ({agent.input_tokens + agent.output_tokens} tokens)")
        print(f"  Log: {agent.log_file}")
    else:
        await db.update_review(review.id, status=ReviewStatus.FAILED, error=agent.error)
        print(f"\nReview failed: {agent.error}")
        return 1

    await db.close()
    return 0


async def _run_server(config: Config) -> None:
    """Run the MCP server + review engine."""
    from .connectors.nats_client import NatsClient
    from .project_registry import build_registry
    from .review_engine import ReviewEngine
    from .server import create_server

    db = _create_db(config)
    await db.connect()

    projects = build_registry(config.projects_file)

    # Optional NATS
    nats_client = None
    if config.nats_enabled:
        from .connectors.nats_config import NatsConfig

        nats_client = NatsClient()
        await nats_client.connect(NatsConfig.from_env())

    # Create MCP server
    mcp = create_server(db, config)

    # Create engine
    engine = ReviewEngine(db, config, projects, nats_client)

    # Run both
    engine_task = asyncio.create_task(engine.start())

    try:
        # Run MCP server (blocks)
        await mcp.run_sse_async(host=config.mcp_host, port=config.mcp_port)
    finally:
        await engine.stop()
        engine_task.cancel()
        if nats_client:
            await nats_client.close()
        await db.close()


async def _show_status(config: Config) -> None:
    """Show recent review status."""
    db = _create_db(config)
    await db.connect()

    reviews = await db.get_reviews(limit=20)
    if not reviews:
        print("No reviews found.")
        await db.close()
        return

    print(f"\n{'ID':<10} {'Project':<20} {'Status':<14} {'Verdict':<18} {'Comments':<10} {'MR'}")
    print("-" * 100)
    for r in reviews:
        print(f"{r.id:<10} {r.project:<20} {r.status:<14} {r.verdict or '-':<18} {r.comments_posted:<10} {r.mr_url[:40]}")

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


def main():
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="minion",
        description="Minion Suite — AI agent suite starting with code review",
    )
    subparsers = parser.add_subparsers(dest="command")

    # minion review <url>
    review_parser = subparsers.add_parser("review", help="Review a merge/pull request")
    review_parser.add_argument("mr_url", help="MR/PR URL to review")
    review_parser.add_argument("--project", "-p", help="Project name from projects.yaml")

    # Global flags
    parser.add_argument("--server", action="store_true", help="Run MCP server + review engine")
    parser.add_argument("--preflight", action="store_true", help="Run health checks")
    parser.add_argument("--status", action="store_true", help="Show recent review status")
    parser.add_argument("--costs", action="store_true", help="Show cost summary")
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

    if args.costs:
        asyncio.run(_show_costs(config, args.global_project))
        return

    if args.server:
        asyncio.run(_run_server(config))
        return

    if args.command == "review":
        exit_code = asyncio.run(_run_one_shot(args.mr_url, args.project, config))
        sys.exit(exit_code)
        return

    parser.print_help()
    sys.exit(1)


if __name__ == "__main__":
    main()
