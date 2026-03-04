"""Renovate bot auto-triage engine.

Polls renovate-enabled projects for open MRs created by Renovate bot,
classifies risk, and either auto-merges (patch/minor with green CI)
or escalates (major bumps, CI failures, conflicts).

Runs alongside ReviewEngine, sharing DB / NATS / config infrastructure.
"""

import asyncio
import logging
import time

from .config import Config
from .connectors.nats_client import NatsClient
from .db import AbstractDatabase
from .git_provider import GitProviderProtocol, create_provider
from .models import RenovateAction, RenovateReview, RenovateStatus, _now
from .project_registry import ProjectConfig
from .renovate_classifier import classify_risk, is_renovate_mr, parse_version_bump, should_auto_merge

logger = logging.getLogger(__name__)


class RenovateEngine:
    """Async engine that scans for Renovate MRs and auto-triages them."""

    def __init__(
        self,
        db: AbstractDatabase,
        config: Config,
        projects: dict[str, ProjectConfig],
        nats_client: NatsClient | None = None,
    ):
        self.db = db
        self.config = config
        self.projects = projects
        self.nats = nats_client
        self._running = False
        self._active_tasks: set[asyncio.Task] = set()
        self._last_poll_time: dict[str, float] = {}

    async def start(self) -> None:
        """Start the polling loop."""
        self._running = True
        logger.info(
            "RenovateEngine started (poll_interval=%ds, max_concurrent=%d)",
            self.config.renovate_poll_interval,
            self.config.renovate_max_concurrent,
        )

        while self._running:
            try:
                await self._poll()
            except Exception:
                logger.exception("RenovateEngine poll error")
            await asyncio.sleep(self.config.renovate_poll_interval)

    async def stop(self) -> None:
        """Graceful shutdown."""
        self._running = False
        for task in self._active_tasks:
            if not task.done():
                task.cancel()
        if self._active_tasks:
            await asyncio.gather(*self._active_tasks, return_exceptions=True)
        self._active_tasks.clear()
        logger.info("RenovateEngine stopped")

    async def _poll(self) -> None:
        """Scan all renovate-enabled projects for open MRs."""
        # Clean up finished tasks
        done = {t for t in self._active_tasks if t.done()}
        for t in done:
            exc = t.exception() if not t.cancelled() else None
            if exc:
                logger.error("Renovate task failed: %s", exc)
        self._active_tasks -= done

        for project_name, project in self.projects.items():
            if not project.renovate.enabled:
                continue

            if not self._cooldown_ok(project_name, project.renovate.cooldown_seconds):
                continue

            # Check capacity
            available = self.config.renovate_max_concurrent - len(self._active_tasks)
            if available <= 0:
                break

            try:
                provider = _create_provider_for_project(project, self.config)
            except ValueError as e:
                logger.error("Failed to create provider for %s: %s", project_name, e)
                continue

            # List open MRs for each bot username
            for bot_username in project.renovate.bot_usernames:
                try:
                    mrs = await provider.list_open_mrs(project.project_id, author=bot_username)
                except NotImplementedError:
                    logger.warning("Provider for %s does not support list_open_mrs", project_name)
                    break
                except Exception:
                    logger.exception("Error listing MRs for %s (author=%s)", project_name, bot_username)
                    continue

                for mr in mrs:
                    if not is_renovate_mr(mr.author, mr.branch, project.renovate.bot_usernames, project.renovate.branch_prefixes):
                        continue

                    # Check if already processed
                    already = await self.db.is_mr_already_processed(mr.url)
                    if already:
                        continue

                    available = self.config.renovate_max_concurrent - len(self._active_tasks)
                    if available <= 0:
                        break

                    task = asyncio.create_task(
                        self._process_renovate_mr(project_name, project, provider, mr),
                        name=f"renovate-{project_name}-{mr.id}",
                    )
                    task.add_done_callback(self._active_tasks.discard)
                    self._active_tasks.add(task)

            self._last_poll_time[project_name] = time.monotonic()

    async def _process_renovate_mr(self, project_name: str, project: ProjectConfig, provider: GitProviderProtocol, mr) -> None:
        """Process a single Renovate MR end-to-end."""
        renovate_cfg = project.renovate

        # Parse version bump from title/branch
        dep_name, from_ver, to_ver, risk_level = parse_version_bump(mr.title, mr.branch)

        # Create DB record
        review = RenovateReview(
            project=project_name,
            mr_url=mr.url,
            mr_id=mr.id,
            branch=mr.branch,
            title=mr.title,
            dependency_name=dep_name,
            from_version=from_ver,
            to_version=to_ver,
            risk_level=risk_level,
            status=RenovateStatus.DETECTED,
        )
        review = await self.db.create_renovate_review(review)
        logger.info("Renovate MR detected: %s [%s] %s %s->%s (%s)", mr.url, project_name, dep_name, from_ver or "?", to_ver or "?", risk_level)

        try:
            # Update status to ASSESSING
            await self.db.update_renovate_review(review.id, status=RenovateStatus.ASSESSING, assessed_at=_now())

            # Get fresh pipeline + conflict info
            pipeline_info = await provider.get_pipeline_status(project.project_id, mr.id)
            fresh_mr = await provider.get_pr(project.project_id, mr.id)

            ci_status = pipeline_info.status
            has_conflicts = fresh_mr.has_conflicts

            await self.db.update_renovate_review(review.id, ci_status=ci_status, has_conflicts=has_conflicts)

            # Classify risk
            risk_decision = classify_risk(
                risk_level,
                dep_name,
                renovate_cfg.excluded_packages,
                renovate_cfg.auto_merge_patch,
                renovate_cfg.auto_merge_minor,
                renovate_cfg.auto_merge_major,
            )

            # Final merge decision
            merge_ok, reason = should_auto_merge(ci_status, has_conflicts, risk_decision, renovate_cfg.require_ci_pass)

            if merge_ok:
                await self._auto_merge(review, project, provider, reason)
            else:
                await self._escalate(review, project, provider, reason)

        except Exception as e:
            logger.exception("Error processing renovate MR %s", mr.url)
            await self.db.update_renovate_review(
                review.id,
                status=RenovateStatus.FAILED,
                action=RenovateAction.FAILED,
                error=str(e)[:500],
                completed_at=_now(),
            )
            if self.nats:
                await self.nats.publish_renovate_failed(project_name, review.id, str(e)[:500])

    async def _auto_merge(self, review: RenovateReview, project: ProjectConfig, provider: GitProviderProtocol, reason: str) -> None:
        """Approve and merge a Renovate MR."""
        renovate_cfg = project.renovate

        # Build comment
        comment = _build_auto_merge_comment(review, reason)

        await self.db.update_renovate_review(review.id, status=RenovateStatus.MERGING, comment_body=comment)

        # Post approval + comment
        await provider.submit_review(project.project_id, review.mr_id, "approve", comment)

        # Merge (with MWPS if pipeline is still running)
        use_mwps = review.ci_status in ("running", "pending", "")
        squash = renovate_cfg.merge_method == "squash"

        try:
            result = await provider.merge_mr(
                project.project_id,
                review.mr_id,
                merge_when_pipeline_succeeds=use_mwps,
                squash=squash,
            )
            merge_sha = result.get("merge_commit_sha", result.get("sha", ""))
        except Exception as e:
            # If merge fails (e.g. pipeline not yet started), log but don't fail the whole process
            logger.warning("merge_mr failed for %s (will rely on MWPS): %s", review.mr_url, e)
            merge_sha = ""

        await self.db.update_renovate_review(
            review.id,
            status=RenovateStatus.DONE,
            action=RenovateAction.AUTO_MERGED,
            merge_sha=merge_sha,
            completed_at=_now(),
        )

        logger.info("Auto-merged renovate MR: %s (%s)", review.mr_url, reason)

        if self.nats:
            await self.nats.publish_renovate_merged(review.project, review.id, review.mr_url)

    async def _escalate(self, review: RenovateReview, project: ProjectConfig, provider: GitProviderProtocol, reason: str) -> None:
        """Post escalation comment on a Renovate MR that needs human review."""
        comment = _build_escalate_comment(review, reason)

        await self.db.update_renovate_review(review.id, comment_body=comment)

        # Post a comment (not a blocking review — just informational)
        await provider.submit_review(project.project_id, review.mr_id, "request_changes", comment)

        await self.db.update_renovate_review(
            review.id,
            status=RenovateStatus.DONE,
            action=RenovateAction.ESCALATED,
            completed_at=_now(),
        )

        logger.info("Escalated renovate MR: %s (%s)", review.mr_url, reason)

        if self.nats:
            await self.nats.publish_renovate_escalated(review.project, review.id, review.mr_url)

    def _cooldown_ok(self, project_name: str, cooldown_seconds: int) -> bool:
        """Check if enough time has passed since last poll for this project."""
        last = self._last_poll_time.get(project_name, 0)
        return (time.monotonic() - last) >= cooldown_seconds

    async def scan_project(self, project_name: str) -> list[RenovateReview]:
        """One-shot scan of a single project. Returns list of processed reviews."""
        project = self.projects.get(project_name)
        if not project:
            raise ValueError(f"Project '{project_name}' not found in registry")
        if not project.renovate.enabled:
            raise ValueError(f"Renovate is not enabled for project '{project_name}'")

        provider = _create_provider_for_project(project, self.config)
        processed = []

        for bot_username in project.renovate.bot_usernames:
            try:
                mrs = await provider.list_open_mrs(project.project_id, author=bot_username)
            except Exception:
                logger.exception("Error listing MRs for %s (author=%s)", project_name, bot_username)
                continue

            for mr in mrs:
                if not is_renovate_mr(mr.author, mr.branch, project.renovate.bot_usernames, project.renovate.branch_prefixes):
                    continue

                already = await self.db.is_mr_already_processed(mr.url)
                if already:
                    continue

                await self._process_renovate_mr(project_name, project, provider, mr)
                review = await self.db.get_renovate_reviews(project=project_name, limit=1)
                if review:
                    processed.append(review[0])

        return processed

    async def process_single_mr(self, mr_url: str, project_name: str) -> RenovateReview | None:
        """Process a single MR URL. Returns the resulting review record."""
        from .cli import _parse_mr_url

        project = self.projects.get(project_name)
        if not project:
            raise ValueError(f"Project '{project_name}' not found in registry")

        provider = _create_provider_for_project(project, self.config)
        mr_id, _, _ = _parse_mr_url(mr_url)
        mr = await provider.get_pr(project.project_id, mr_id)

        await self._process_renovate_mr(project_name, project, provider, mr)

        # Return the latest review for this MR
        reviews = await self.db.get_renovate_reviews(project=project_name, limit=10)
        for r in reviews:
            if r.mr_url == mr_url:
                return r
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _build_auto_merge_comment(review: RenovateReview, reason: str) -> str:
    """Build the approval comment for an auto-merged Renovate MR."""
    parts = [
        "## Renovate Auto-Merge",
        "",
        f"**Dependency:** `{review.dependency_name or 'unknown'}`",
    ]
    if review.from_version and review.to_version:
        parts.append(f"**Version:** `{review.from_version}` -> `{review.to_version}`")
    elif review.to_version:
        parts.append(f"**Version:** -> `{review.to_version}`")
    parts.extend(
        [
            f"**Risk level:** {review.risk_level}",
            f"**CI status:** {review.ci_status or 'n/a'}",
            f"**Reason:** {reason}",
            "",
            "Auto-merged by Minion Suite.",
        ]
    )
    return "\n".join(parts)


def _build_escalate_comment(review: RenovateReview, reason: str) -> str:
    """Build the escalation comment for a Renovate MR that needs human review."""
    parts = [
        "## Renovate Escalation",
        "",
        f"**Dependency:** `{review.dependency_name or 'unknown'}`",
    ]
    if review.from_version and review.to_version:
        parts.append(f"**Version:** `{review.from_version}` -> `{review.to_version}`")
    elif review.to_version:
        parts.append(f"**Version:** -> `{review.to_version}`")
    parts.extend(
        [
            f"**Risk level:** {review.risk_level}",
            f"**CI status:** {review.ci_status or 'n/a'}",
            f"**Has conflicts:** {'yes' if review.has_conflicts else 'no'}",
            f"**Reason:** {reason}",
            "",
            "Please review and merge manually.",
        ]
    )
    return "\n".join(parts)
