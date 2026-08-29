"""GitLab issues poller that creates development jobs from labeled issues."""

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx

from ..config import Config
from ..core.models import Job, JobStatus
from ..db import AbstractDatabase
from ..project_registry import ProjectConfig

logger = logging.getLogger(__name__)

TERMINAL_STATUSES = {JobStatus.DONE, JobStatus.FAILED, JobStatus.NO_WORK_NEEDED}

LABEL_IN_PROGRESS = "minion-in-progress"
LABEL_DONE = "minion-done"
LABEL_FAILED = "minion-failed"


class GitLabIssuesPoller:
    """Polls GitLab projects for issues with a trigger label, creates jobs from them."""

    def __init__(self, config: Config, db: AbstractDatabase, projects: dict[str, ProjectConfig], dry_run: bool = False):
        self.config = config
        self.db = db
        self.projects = projects
        self.dry_run = dry_run
        self._client: httpx.AsyncClient | None = None
        self._running = False
        # Keyed by "{project_id}:{issue_iid}"
        self._active: dict[str, dict[str, Any]] = {}
        # Liveness stamp read by the poller watchdog in cli.py. Advanced only
        # after a poll cycle returns without raising, so a poller that fails
        # every cycle goes stale instead of looking busy. Seeded at construction
        # so the first cycle gets a full window before the watchdog judges it.
        self.last_poll_at = time.monotonic()

    @property
    def poll_interval(self) -> int:
        """Seconds between poll cycles, under the name the watchdog reads.

        Each poller names its own config key; the watchdog wants one name.
        """
        return self.config.gitlab_issues_poll_interval

    async def start(self):
        """Main polling loop."""
        self._client = httpx.AsyncClient(timeout=30.0)
        try:
            await self._rehydrate_active()
            self._running = True
            mode = " (DRY RUN)" if self.dry_run else ""
            logger.info(
                "GitLab issues poller started%s -- %d project(s) with issues enabled",
                mode,
                sum(1 for p in self.projects.values() if p.issues.enabled),
            )
            while self._running:
                try:
                    await self._poll()
                    # Reached only when the cycle actually completed. Stamping
                    # before the call, or in a `finally`, would make a poller
                    # that raises every cycle look identical to a healthy one.
                    self.last_poll_at = time.monotonic()
                except httpx.HTTPError as e:
                    logger.error("GitLab API error during poll: %s", e)
                except Exception:
                    logger.exception("Unexpected error in GitLab issues poll cycle")
                await asyncio.sleep(self.config.gitlab_issues_poll_interval)
        finally:
            await self._client.aclose()

    async def stop(self):
        """Graceful shutdown."""
        self._running = False

    async def _rehydrate_active(self):
        """Restore _active from DB on startup."""
        active_jobs = await self.db.get_active_jobs()
        for job in active_jobs:
            if job.external_id and job.external_id not in self._active:
                self._active[job.external_id] = {
                    "job_id": job.id,
                    "started_at": job.created_at,
                    "issue_title": f"(rehydrated job={job.id[:8]})",
                }
        if self._active:
            logger.info("Rehydrated %d active GitLab issue job(s) from DB", len(self._active))

    async def _poll(self):
        """One poll cycle: monitor running jobs, then check for new issues."""
        await self._monitor_jobs()

        db_active = await self.db.get_active_jobs()
        active_count = max(len(self._active), len(db_active))

        if active_count >= self.config.max_concurrent_jobs:
            return

        slots = self.config.max_concurrent_jobs - active_count

        for name, project in self.projects.items():
            if not project.issues.enabled:
                continue
            if slots <= 0:
                break

            gitlab_url = project.gitlab_url or self.config.gitlab_url
            if not gitlab_url:
                logger.warning("Project %s has issues enabled but no gitlab_url", name)
                continue

            issues = await self._fetch_issues(gitlab_url, project.project_id, project.issues.label)
            for issue in issues:
                if slots <= 0:
                    break
                issue_key = f"{project.project_id}:{issue['iid']}"
                if issue_key in self._active:
                    continue
                # Check DB for idempotency
                existing = await self.db.get_job_by_external_id(issue_key)
                if existing and existing.status not in TERMINAL_STATUSES:
                    self._active[issue_key] = {
                        "job_id": existing.id,
                        "started_at": existing.created_at,
                        "issue_title": issue.get("title", "?"),
                        "project_name": name,
                        "project_id": project.project_id,
                        "gitlab_url": gitlab_url,
                        "issue_iid": issue["iid"],
                        "trigger_label": project.issues.label,
                    }
                    continue

                await self._launch_job(name, project, gitlab_url, issue)
                slots -= 1

    async def _fetch_issues(self, gitlab_url: str, project_id: str, label: str) -> list[dict]:
        """Fetch open issues with the trigger label from a GitLab project."""
        encoded_id = quote(project_id, safe="")
        url = f"{gitlab_url.rstrip('/')}/api/v4/projects/{encoded_id}/issues"
        params = {"labels": label, "state": "opened", "per_page": 20}
        resp = await self._api("GET", url, params=params)
        resp.raise_for_status()
        return resp.json()

    async def _update_labels(self, gitlab_url: str, project_id: str, issue_iid: int, add: list[str], remove: list[str]):
        """Update labels on a GitLab issue."""
        encoded_id = quote(project_id, safe="")
        url = f"{gitlab_url.rstrip('/')}/api/v4/projects/{encoded_id}/issues/{issue_iid}"

        # Fetch current labels first
        resp = await self._api("GET", url)
        resp.raise_for_status()
        current_labels = set(resp.json().get("labels", []))

        new_labels = (current_labels | set(add)) - set(remove)
        resp = await self._api("PUT", url, json_body={"labels": ",".join(sorted(new_labels))})
        resp.raise_for_status()

    async def _post_comment(self, gitlab_url: str, project_id: str, issue_iid: int, body: str):
        """Post a note (comment) on a GitLab issue."""
        encoded_id = quote(project_id, safe="")
        url = f"{gitlab_url.rstrip('/')}/api/v4/projects/{encoded_id}/issues/{issue_iid}/notes"
        resp = await self._api("POST", url, json_body={"body": body})
        resp.raise_for_status()

    async def _close_issue(self, gitlab_url: str, project_id: str, issue_iid: int):
        """Close a GitLab issue."""
        encoded_id = quote(project_id, safe="")
        url = f"{gitlab_url.rstrip('/')}/api/v4/projects/{encoded_id}/issues/{issue_iid}"
        resp = await self._api("PUT", url, json_body={"state_event": "close"})
        resp.raise_for_status()

    async def _api(self, method: str, url: str, params: dict | None = None, json_body: dict | None = None) -> httpx.Response:
        """Make an authenticated GitLab API call."""
        headers = {"PRIVATE-TOKEN": self.config.gitlab_token}
        return await self._client.request(method, url, params=params, json=json_body, headers=headers)

    async def _launch_job(self, project_name: str, project: ProjectConfig, gitlab_url: str, issue: dict):
        """Create a job from a GitLab issue."""
        issue_iid = issue["iid"]
        issue_title = issue.get("title", "")
        issue_desc = issue.get("description", "") or ""
        trigger_label = project.issues.label
        issue_key = f"{project.project_id}:{issue_iid}"

        spec_text = f"# {issue_title}\n\n{issue_desc}"

        if self.dry_run:
            logger.info("[DRY RUN] Would create job for issue %s (%r) -- spec=%d chars", issue_key, issue_title, len(spec_text))
            logger.info("[DRY RUN] Would update labels: remove=%s add=%s", trigger_label, LABEL_IN_PROGRESS)
            return

        # Update labels: remove trigger, add in-progress
        try:
            await self._update_labels(gitlab_url, project.project_id, issue_iid, add=[LABEL_IN_PROGRESS], remove=[trigger_label])
        except httpx.HTTPError as e:
            logger.error("Failed to update labels on issue %s: %s", issue_key, e)

        job = await self.db.create_job(spec_text, external_id=issue_key)

        started_at = datetime.now(UTC).isoformat()
        self._active[issue_key] = {
            "job_id": job.id,
            "started_at": started_at,
            "issue_title": issue_title,
            "project_name": project_name,
            "project_id": project.project_id,
            "gitlab_url": gitlab_url,
            "issue_iid": issue_iid,
            "trigger_label": trigger_label,
        }

        try:
            await self._post_comment(
                gitlab_url,
                project.project_id,
                issue_iid,
                f"Job created (id={job.id[:8]}, time={started_at})",
            )
        except httpx.HTTPError as e:
            logger.debug("Failed to post comment on issue %s: %s", issue_key, e)

        logger.info("Created job %s for GitLab issue %s (%r)", job.id[:8], issue_key, issue_title)

    async def _monitor_jobs(self):
        """Check DB for completed jobs and update corresponding issues."""
        for issue_key in list(self._active.keys()):
            info = self._active[issue_key]
            job_id = info["job_id"]
            try:
                job = await self.db.get_job(job_id)
                if job and job.status in TERMINAL_STATUSES:
                    await self._handle_completion(issue_key, info, job)
            except Exception:
                logger.exception("Error checking job %s status", job_id[:8])

    async def _handle_completion(self, issue_key: str, info: dict, job: Job):
        """Handle a completed job: update labels, post comment, optionally close issue."""
        issue_title = info.get("issue_title", "?")
        started_at = info.get("started_at", "")
        elapsed = _format_elapsed(started_at)
        gitlab_url = info.get("gitlab_url", self.config.gitlab_url)
        project_id = info.get("project_id", "")
        issue_iid = info.get("issue_iid")

        if not issue_iid or not project_id or not gitlab_url:
            logger.warning("Incomplete info for issue %s, skipping update", issue_key)
            del self._active[issue_key]
            return

        if job.status in (JobStatus.DONE, JobStatus.NO_WORK_NEEDED):
            done_label = LABEL_DONE
            status_text = "completed successfully"
        else:
            done_label = LABEL_FAILED
            status_text = f"failed ({job.error or 'unknown error'})"

        if self.dry_run:
            logger.info("[DRY RUN] Job %s for issue %s (%r) %s (%s)", job.id[:8], issue_key, issue_title, status_text, elapsed)
            logger.info("[DRY RUN] Would update labels: remove=%s add=%s", LABEL_IN_PROGRESS, done_label)
            if job.status in (JobStatus.DONE, JobStatus.NO_WORK_NEEDED):
                logger.info("[DRY RUN] Would close issue %s", issue_key)
            del self._active[issue_key]
            return

        # Update labels
        try:
            await self._update_labels(gitlab_url, project_id, issue_iid, add=[done_label], remove=[LABEL_IN_PROGRESS])
        except httpx.HTTPError as e:
            logger.error("Failed to update labels on issue %s: %s", issue_key, e)

        # Post result comment
        comment_lines = [
            f"Job {status_text}",
            f"Duration: {elapsed}",
        ]
        if job.error:
            comment_lines.append(f"```\n{job.error[:500]}\n```")

        try:
            await self._post_comment(gitlab_url, project_id, issue_iid, "\n".join(comment_lines))
        except httpx.HTTPError as e:
            logger.error("Failed to post result comment on issue %s: %s", issue_key, e)

        # Close issue on success
        if job.status in (JobStatus.DONE, JobStatus.NO_WORK_NEEDED):
            try:
                await self._close_issue(gitlab_url, project_id, issue_iid)
            except httpx.HTTPError as e:
                logger.error("Failed to close issue %s: %s", issue_key, e)

        logger.info("Job %s for issue %s (%r) %s (%s)", job.id[:8], issue_key, issue_title, status_text, elapsed)
        del self._active[issue_key]


def _format_elapsed(started_at: str) -> str:
    """Format elapsed time from an ISO timestamp to now."""
    try:
        start = datetime.fromisoformat(started_at)
        secs = int((datetime.now(UTC) - start).total_seconds())
        if secs < 60:
            return f"{secs}s"
        elif secs < 3600:
            return f"{secs // 60}m {secs % 60}s"
        return f"{secs // 3600}h {(secs % 3600) // 60}m"
    except (ValueError, TypeError):
        return "?"
