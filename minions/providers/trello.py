"""Trello board poller that creates jobs from Trello cards."""

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

import httpx

from ..config import Config
from ..core.models import Job, JobStatus
from ..db import AbstractDatabase

logger = logging.getLogger(__name__)

TRELLO_API = "https://api.trello.com/1"

# List names the poller looks for. Matched case-insensitively against the board: names are
# normalised with .strip().lower() in _resolve_list_ids, so these must be the lowercase form
# of the list's ACTUAL name on the board.
#
# Corrected 2026-07-25. These were aspirational names ("minions-*") that no list on the board
# ever had, so _resolve_list_ids raised on every start and took the whole process down —
# CrashLoopBackOff, since a component death is treated as fatal. Board's real lists are:
# Ideas, Design, On-deck, tech-debt, In progress, Done, Fucked.
#
# LIST_DONE was already correct by accident: "done" is the normalised form of "Done".
LIST_ONDECK = "on-deck"
LIST_IN_PROGRESS = "in progress"
LIST_DONE = "done"
LIST_FAILED = "fucked"

REQUIRED_LISTS = [LIST_ONDECK, LIST_IN_PROGRESS, LIST_DONE, LIST_FAILED]

# Opt-in gate: a card is only eligible for pickup if it carries this label.
# The on-deck column is the team's shared backlog, not a minions queue.
MINION_LABEL = "minion"

TERMINAL_STATUSES = {JobStatus.DONE, JobStatus.FAILED, JobStatus.NO_WORK_NEEDED}


class TrelloPoller:
    def __init__(self, config: Config, db: AbstractDatabase):
        self.config = config
        self.db = db
        self._auth = {"key": config.trello_api_key, "token": config.trello_token}
        self._client: httpx.AsyncClient | None = None
        self._list_ids: dict[str, str] = {}
        self._minion_label_id: str | None = None
        self._running = False
        self._active: dict[str, dict[str, Any]] = {}

    async def start(self):
        """Main polling loop."""
        self._client = httpx.AsyncClient(timeout=30.0)
        try:
            await self._resolve_list_ids()
            await self._resolve_minion_label()
            await self._rehydrate_active()
            self._running = True
            logger.info(
                "Trello poller started -- board=%s poll=%ds",
                self.config.trello_board_id,
                self.config.trello_poll_interval,
            )
            while self._running:
                try:
                    await self._poll()
                except httpx.HTTPError as e:
                    logger.error("Trello API error during poll: %s", e)
                except Exception:
                    logger.exception("Unexpected error in poll cycle")
                await asyncio.sleep(self.config.trello_poll_interval)
        finally:
            await self._client.aclose()

    async def stop(self):
        """Graceful shutdown."""
        self._running = False

    async def _resolve_list_ids(self):
        """Fetch lists from the board and map names to IDs."""
        resp = await self._api("GET", f"/boards/{self.config.trello_board_id}/lists", params={"fields": "name"})
        resp.raise_for_status()
        lists = resp.json()

        for lst in lists:
            normalized = lst["name"].strip().lower()
            self._list_ids[normalized] = lst["id"]

        missing = [name for name in REQUIRED_LISTS if name not in self._list_ids]
        if missing:
            raise RuntimeError(f"Missing required Trello lists: {missing}. Found: {[lst['name'] for lst in lists]}")

        logger.info("Resolved %d lists from board", len(self._list_ids))

    async def _resolve_minion_label(self):
        """Find or create the 'minion' label on the board."""
        resp = await self._api("GET", f"/boards/{self.config.trello_board_id}/labels", params={"fields": "name,color"})
        resp.raise_for_status()
        labels = resp.json()

        for label in labels:
            if label.get("name", "").strip().lower() == MINION_LABEL:
                self._minion_label_id = label["id"]
                return

        resp = await self._api(
            "POST",
            f"/boards/{self.config.trello_board_id}/labels",
            params={"name": MINION_LABEL, "color": "purple"},
        )
        resp.raise_for_status()
        self._minion_label_id = resp.json()["id"]

    async def _rehydrate_active(self):
        """Restore _active from DB on startup."""
        active_jobs = await self.db.get_active_jobs()
        for job in active_jobs:
            if job.external_id and job.external_id not in self._active:
                self._active[job.external_id] = {
                    "job_id": job.id,
                    "started_at": job.created_at,
                    "card_name": f"(rehydrated job={job.id[:8]})",
                }
        if self._active:
            logger.info("Rehydrated %d active job(s) from DB", len(self._active))

    async def _poll(self):
        """One poll cycle: check for new cards, monitor running jobs."""
        await self._monitor_jobs()

        db_active = await self.db.get_active_jobs()
        active_count = max(len(self._active), len(db_active))

        if active_count >= self.config.max_concurrent_jobs:
            return

        cards = await self._get_cards(self._list_ids[LIST_ONDECK], require_minion_label=self.config.trello_require_label)
        if not cards:
            return

        slots = self.config.max_concurrent_jobs - active_count
        for card in cards[:slots]:
            await self._launch_job(card)

    async def _get_cards(self, list_id: str, require_minion_label: bool = False) -> list:
        """Fetch cards from a Trello list.

        With require_minion_label, only cards carrying the `minion` label are
        returned — an opt-in gate on what minions is allowed to pick up.

        This matters because the on-deck list is the team's shared backlog, not a
        minions queue. The label was previously written on pickup and never read,
        so every card in the column was fair game: the poller took the whole
        backlog, including firmware, LoRa and KMS tickets it has no toolchain for
        and could only fail at, one ceiling's worth of spend at a time.
        """
        resp = await self._api("GET", f"/lists/{list_id}/cards", params={"fields": "name,desc,id,labels"})
        resp.raise_for_status()
        cards = resp.json()

        if not require_minion_label:
            return cards

        return [c for c in cards if any((label.get("name") or "").lower() == MINION_LABEL for label in c.get("labels", []))]

    async def _move_card(self, card_id: str, list_name: str):
        """Move a card to a different list."""
        list_id = self._list_ids[list_name]
        resp = await self._api("PUT", f"/cards/{card_id}", params={"idList": list_id})
        resp.raise_for_status()

    async def _add_comment(self, card_id: str, text: str):
        """Add a comment to a Trello card."""
        resp = await self._api("POST", f"/cards/{card_id}/actions/comments", params={"text": text})
        resp.raise_for_status()

    async def _api(self, method: str, path: str, params: dict | None = None) -> httpx.Response:
        """Make an authenticated Trello API call."""
        url = f"{TRELLO_API}{path}"
        all_params = {**self._auth, **(params or {})}
        return await self._client.request(method, url, params=all_params)

    async def _launch_job(self, card: dict):
        """Create a job from a Trello card."""
        card_id = card["id"]
        card_name = card["name"]
        card_desc = card.get("desc", "")
        spec_text = f"# {card_name}\n\n{card_desc}"

        # Create the job BEFORE touching the card.
        #
        # This was the other way round, and create_job was being handed a Job
        # where it takes a spec string — the same signature bug as submit_spec.
        # So every card was moved out of on-deck, then job creation raised, then
        # the poll loop's `except Exception` swallowed it and carried on. Each
        # cycle drained the next batch: 24 cards moved to "in progress" with no
        # job, no agent and no spend behind any of them, and no way to tell from
        # the board that nothing was happening.
        #
        # Creating the job first means a failure here leaves the card exactly
        # where it was, so the next poll retries it instead of losing it.
        job = await self.db.create_job(spec_text, external_id=card_id)

        await self._move_card(card_id, LIST_IN_PROGRESS)
        if self._minion_label_id:
            try:
                resp = await self._api("POST", f"/cards/{card_id}/idLabels", params={"value": self._minion_label_id})
                resp.raise_for_status()
            except httpx.HTTPError:
                logger.debug("Failed to add minion label to card %s", card_id[:8], exc_info=True)

        started_at = datetime.now(UTC).isoformat()
        self._active[card_id] = {
            "job_id": job.id,
            "started_at": started_at,
            "card_name": card_name,
        }
        await self._add_comment(card_id, f"Job created (id={job.id[:8]}, time={started_at})")
        logger.info("Created job %s for card %s (%r)", job.id[:8], card_id[:8], card_name)

    async def _monitor_jobs(self):
        """Check DB for completed jobs."""
        for card_id in list(self._active.keys()):
            info = self._active[card_id]
            job_id = info["job_id"]
            try:
                job = await self.db.get_job(job_id)
                if job and job.status in TERMINAL_STATUSES:
                    await self._handle_completion(card_id, info, job)
            except Exception:
                logger.exception("Error checking job %s status", job_id[:8])

    async def _handle_completion(self, card_id: str, info: dict, job: Job):
        """Handle a completed job."""
        card_name = info["card_name"]
        started_at = info["started_at"]
        elapsed = _format_elapsed(started_at)

        if job.status in (JobStatus.DONE, JobStatus.NO_WORK_NEEDED):
            target_list = LIST_DONE
            status_text = "completed successfully"
        else:
            target_list = LIST_FAILED
            status_text = f"failed ({job.error or 'unknown error'})"

        try:
            await self._move_card(card_id, target_list)
        except httpx.HTTPError as e:
            logger.error("Failed to move card %s: %s", card_id[:8], e)

        comment_lines = [
            f"Job {status_text}",
            f"Duration: {elapsed}",
        ]
        if job.error:
            comment_lines.append(f"```\n{job.error[:500]}\n```")

        try:
            await self._add_comment(card_id, "\n".join(comment_lines))
        except httpx.HTTPError as e:
            logger.error("Failed to add result comment to card %s: %s", card_id[:8], e)

        logger.info("Job %s for card %s (%r) %s (%s)", job.id[:8], card_id[:8], card_name, status_text, elapsed)
        del self._active[card_id]


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
