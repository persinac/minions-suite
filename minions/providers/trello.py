"""Trello board poller that creates jobs from Trello cards."""

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta
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
        # Edge-triggered, so the "nothing is labelled" warning is logged on the
        # transition into that state rather than on every poll. See _get_cards.
        self._label_gate_warned = False
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
        return self.config.trello_poll_interval

    async def start(self):
        """Main polling loop."""
        self._client = httpx.AsyncClient(timeout=30.0)
        try:
            await self._resolve_list_ids()
            await self._resolve_minion_label()
            await self._rehydrate_active()
            await self._reconcile_stranded_cards()
            self._running = True
            logger.info(
                "Trello poller started -- board=%s poll=%ds",
                self.config.trello_board_id,
                self.config.trello_poll_interval,
            )
            while self._running:
                try:
                    await self._poll()
                    # Reached only when the cycle actually completed. Stamping
                    # before the call, or in a `finally`, would make a poller
                    # that raises every cycle look identical to a healthy one.
                    self.last_poll_at = time.monotonic()
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

    async def _reconcile_stranded_cards(self):
        """Move cards whose job finished while this poller was not watching.

        _monitor_jobs walks only the in-memory _active dict, and
        _rehydrate_active can repopulate it solely from get_active_jobs() --
        which excludes terminal jobs by definition. So a job that reaches its
        terminal state while this process is down is invisible to the poller
        forever: nothing revisits the card, and it sits in "In progress" with
        no mechanism left that could ever move it.

        Not hypothetical, and not rare. Card 1MJtZ4rq sat there for 24 hours
        after job 43a3e937 merged PR #142, with no card-move event recorded at
        all, and the 2026-08-19 checkpoint carries an earlier one. Every
        release restarts this process, and nine shipped on 2026-08-23 alone --
        each one an open window.

        Startup is the right moment: the window this closes is the restart
        that just happened, the in-progress list is small, and it runs once.

        Ownership is decided by the DATABASE, not the label: a card minions
        never picked up has no job row, so it cannot be touched here, while a
        card whose label a human stripped is still recovered. Reading the list
        unfiltered also keeps the label-gate warning -- which names on-deck --
        from firing about the wrong list.
        """
        try:
            cards = await self._get_cards(self._list_ids[LIST_IN_PROGRESS])
        except httpx.HTTPError as e:
            # Never fatal: start() raising takes the whole process down, and a
            # stranded card is worth strictly less than a running poller.
            logger.error("Could not read %s to reconcile stranded cards: %s", LIST_IN_PROGRESS, e)
            return

        reconciled = 0
        for card in cards:
            card_id = card.get("id")
            if not card_id or card_id in self._active:
                # Already rehydrated: the job is still running and the normal
                # monitor owns it.
                continue
            try:
                job = await self.db.get_job_by_external_id(card_id)
            except Exception:
                logger.exception("Could not look up the job for card %s", card_id[:8])
                continue
            if not job or job.status not in TERMINAL_STATUSES:
                continue

            logger.warning(
                "Card %s (%r) was stranded in %s: job %s finished as %s while nothing was watching -- moving it now",
                card_id[:8],
                card.get("name", "")[:60],
                LIST_IN_PROGRESS,
                job.id[:8],
                job.status,
            )
            self._active[card_id] = {
                "job_id": job.id,
                "started_at": job.created_at,
                "card_name": card.get("name", f"(job={job.id[:8]})"),
            }
            try:
                await self._handle_completion(card_id, self._active[card_id], job)
                reconciled += 1
            except Exception:
                logger.exception("Could not reconcile stranded card %s", card_id[:8])
                self._active.pop(card_id, None)

        if reconciled:
            logger.info("Reconciled %d stranded card(s) on startup", reconciled)

    async def _intake_interval_elapsed(self) -> bool:
        """Whether enough time has passed since the last job to admit another.

        Throttles how fast the queue is drained, independently of how often we
        poll — `_poll` also monitors running jobs and moves their cards, so
        slowing the whole loop to cap spend would strand finished cards in
        "In progress" for hours.

        Measured against job creation time in the database rather than an
        in-process timestamp: a pod restart must not reset the clock and let a
        job through early. Counts ALL jobs in the window, including ones
        submitted over MCP, so a manual submission also pushes the queue's next
        pickup out — the point is to cap total spend, not to cap one source.
        """
        interval = self.config.trello_min_job_interval
        if interval <= 0:
            return True

        since = (datetime.now(UTC) - timedelta(seconds=interval)).isoformat()
        recent = await self.db.count_jobs_since(since)
        if recent > 0:
            logger.debug("Trello intake throttled: %d job(s) started within the last %ds", recent, interval)
            return False
        return True

    async def _poll(self):
        """One poll cycle: check for new cards, monitor running jobs."""
        await self._monitor_jobs()

        db_active = await self.db.get_active_jobs()
        active_count = max(len(self._active), len(db_active))

        if active_count >= self.config.max_concurrent_jobs:
            return

        if not await self._intake_interval_elapsed():
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

        eligible = [c for c in cards if any((label.get("name") or "").lower() == MINION_LABEL for label in c.get("labels", []))]

        # A full board and an empty queue look identical from outside, and this
        # is the *normal* steady state once the label gate is on: on-deck is the
        # team's backlog, so it is never empty, and the poller silently does
        # nothing until somebody labels a card. On 2026-07-28 that state had
        # held for days across 26 cards with not one log line describing it —
        # every other explanation (throttling, concurrency, a crashed poller)
        # writes a log, so silence pointed everywhere except the real cause.
        #
        # Edge-triggered on purpose. _intake_interval_elapsed only throttles
        # once a job has actually STARTED, so while nothing is eligible it
        # returns True on every cycle and this runs at the raw poll interval —
        # 180s. Warning unconditionally would emit ~480 identical lines a day
        # and become the noise it exists to cut through.
        if cards and not eligible:
            if not self._label_gate_warned:
                logger.warning(
                    "Trello intake is idle: %d card(s) in %s, none carrying the %r label. Nothing will be picked up until a card is labelled.",
                    len(cards),
                    LIST_ONDECK,
                    MINION_LABEL,
                )
                self._label_gate_warned = True
        elif eligible:
            if self._label_gate_warned:
                logger.info("Trello intake resumed: %d eligible card(s)", len(eligible))
            self._label_gate_warned = False

        return eligible

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
    except ValueError, TypeError:
        return "?"
