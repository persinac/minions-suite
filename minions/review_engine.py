"""Review engine — polling loop that picks up queued reviews and runs agents.

Much simpler than mcp-minions' JobEngine: 4 states instead of 8,
no multi-agent coordination, no arbiter.

States: QUEUED -> IN_PROGRESS -> COMMENTED -> DONE (or FAILED)
"""

import asyncio
import logging
from typing import Optional

from .agent import run_review
from .config import Config
from .connectors.nats_client import NatsClient
from .db import AbstractDatabase
from .git_provider import GitProviderProtocol, create_provider
from .models import ReviewStatus, _now
from .project_registry import ProjectConfig

logger = logging.getLogger(__name__)


class ReviewEngine:
    """Async engine that processes queued reviews."""

    def __init__(
        self,
        db: AbstractDatabase,
        config: Config,
        projects: dict[str, ProjectConfig],
        nats_client: Optional[NatsClient] = None,
    ):
        self.db = db
        self.config = config
        self.projects = projects
        self.nats = nats_client
        self._running = False
        self._active_reviews: set[asyncio.Task] = set()

    async def start(self) -> None:
        """Start the engine polling loop."""
        self._running = True
        logger.info("ReviewEngine started (poll_interval=%ds, max_concurrent=%d)", self.config.engine_poll_interval, self.config.max_concurrent_reviews)

        # Subscribe to NATS review requests if available
        if self.nats and self.nats.is_connected:
            await self.nats.subscribe("reviews.requested.>", self._on_nats_review_request)

        while self._running:
            try:
                await self._poll()
            except Exception:
                logger.exception("Engine poll error")
            await asyncio.sleep(self.config.engine_poll_interval)

    async def stop(self) -> None:
        """Stop the engine and cancel active reviews."""
        self._running = False
        for task in self._active_reviews:
            if not task.done():
                task.cancel()
        if self._active_reviews:
            await asyncio.gather(*self._active_reviews, return_exceptions=True)
        self._active_reviews.clear()
        logger.info("ReviewEngine stopped")

    async def _poll(self) -> None:
        """Check for queued reviews and launch agents."""
        # Clean up finished tasks
        done = {t for t in self._active_reviews if t.done()}
        for t in done:
            if t.exception():
                logger.error("Review task failed: %s", t.exception())
        self._active_reviews -= done

        # Check capacity
        available = self.config.max_concurrent_reviews - len(self._active_reviews)
        if available <= 0:
            return

        # Fetch queued reviews
        queued = await self.db.get_queued_reviews(limit=available)
        for review in queued:
            task = asyncio.create_task(
                self._process_review(review.id),
                name=f"review-{review.id}",
            )
            task.add_done_callback(self._active_reviews.discard)
            self._active_reviews.add(task)

    async def _process_review(self, review_id: str) -> None:
        """Process a single review: fetch MR, run agent, update status."""
        review = await self.db.get_review(review_id)
        if not review:
            logger.error("Review %s not found", review_id)
            return

        project = self.projects.get(review.project)
        if not project:
            logger.error("Project %s not found in registry", review.project)
            await self.db.update_review(review_id, status=ReviewStatus.FAILED, error=f"Project '{review.project}' not in projects.yaml")
            return

        # Mark in-progress
        await self.db.update_review(review_id, status=ReviewStatus.IN_PROGRESS, started_at=_now())
        if self.nats:
            await self.nats.publish_review_started(review.project, review.id)

        # Create git provider
        try:
            provider = _create_provider_for_project(project, self.config)
        except ValueError as e:
            logger.error("Failed to create provider for %s: %s", review.project, e)
            await self.db.update_review(review_id, status=ReviewStatus.FAILED, error=str(e))
            return

        # Run the agent
        agent = await run_review(review, project, provider, self.config, self.db)

        # Update review status
        if agent.status == "done":
            await self.db.update_review(
                review_id,
                status=ReviewStatus.DONE,
                verdict=review.verdict,
                summary=review.summary,
                comments_posted=review.comments_posted,
                completed_at=_now(),
            )
            if self.nats:
                await self.nats.publish_review_completed(
                    review.project,
                    review.id,
                    review.verdict or "unknown",
                    review.comments_posted,
                    agent.cost_usd,
                )
        else:
            await self.db.update_review(
                review_id,
                status=ReviewStatus.FAILED,
                error=agent.error,
                completed_at=_now(),
            )
            if self.nats:
                await self.nats.publish_review_failed(review.project, review.id, agent.error or "unknown error")

    async def _on_nats_review_request(self, msg) -> None:
        """Handle a review request from NATS."""
        import json

        try:
            data = json.loads(msg.data.decode("utf-8"))
            mr_url = data.get("mr_url", "")
            project_name = data.get("project", "")

            if not mr_url or not project_name:
                logger.warning("Invalid NATS review request: missing mr_url or project")
                return

            project = self.projects.get(project_name)
            if not project:
                logger.warning("NATS review request for unknown project: %s", project_name)
                return

            # Parse MR ID from URL (simple heuristic)
            mr_id = mr_url.rstrip("/").split("/")[-1]

            from .models import Review

            review = Review(project=project_name, mr_url=mr_url, mr_id=mr_id)
            review = await self.db.create_review(review)
            logger.info("Queued review %s from NATS: %s", review.id, mr_url)

        except Exception:
            logger.exception("Error handling NATS review request")


def _create_provider_for_project(project: ProjectConfig, config: Config) -> GitProviderProtocol:
    """Create the appropriate git provider for a project."""
    provider_type = project.git_provider or config.git_provider

    if provider_type == "gitlab":
        return create_provider(
            "gitlab",
            gitlab_url=project.gitlab_url or config.gitlab_url,
            token=config.gitlab_token,
        )
    if provider_type == "github":
        return create_provider("github", token=config.github_token)

    raise ValueError(f"Unsupported provider: {provider_type}")
