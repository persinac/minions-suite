"""A card whose job finished during a restart must not sit in In progress forever.

_monitor_jobs walks only the in-memory _active dict, and _rehydrate_active can
repopulate it solely from get_active_jobs() — which excludes terminal jobs by
definition. So a job that reached its terminal state while input-sources was
down is invisible to the poller forever: nothing revisits the card, and no
mechanism left in the system can ever move it.

Card 1MJtZ4rq proved it — 24 hours in In progress after job 43a3e937 merged
PR #142, with no card-move event recorded at all — and the 2026-08-19
checkpoint carries an earlier one. Every release restarts this process, so the
window opens on every deploy.

Ownership here is decided by the DATABASE, not the label: a card minions never
picked up has no job row and cannot be touched, while a card whose label a
human stripped is still recovered.
"""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from minions.config import Config
from minions.core.models import JobStatus
from minions.providers.trello import LIST_DONE, LIST_FAILED, LIST_IN_PROGRESS, TrelloPoller

pytestmark = pytest.mark.asyncio

CARD_ID = "6a640dd27d2e2d2f4c23bd28"


def _poller(cards, job_for_card=None, cards_raise=False):
    db = MagicMock()
    db.get_job_by_external_id = AsyncMock(return_value=job_for_card)

    poller = TrelloPoller(Config.from_env(), db)
    poller._list_ids = {LIST_IN_PROGRESS: "inprogress-list", LIST_DONE: "done-list", LIST_FAILED: "fucked-list"}

    async def _get_cards(list_id, require_minion_label=False):
        if cards_raise:
            raise httpx.ConnectError("boom")
        return cards

    poller._get_cards = _get_cards
    poller._move_card = AsyncMock()
    poller._add_comment = AsyncMock()
    return poller


def _job(status=JobStatus.DONE, error=None):
    job = MagicMock()
    job.id = "43a3e937"
    job.status = status
    job.error = error
    job.created_at = "2026-08-22T06:00:00+00:00"
    return job


STRANDED_CARD = [{"id": CARD_ID, "name": "[DR P0] ENFORCE the no-absent-device rule", "desc": "", "labels": [{"name": "minion"}]}]


class TestStrandedCardsAreRecovered:
    async def test_a_finished_jobs_card_is_moved_to_done(self):
        poller = _poller(STRANDED_CARD, job_for_card=_job())

        await poller._reconcile_stranded_cards()

        poller._move_card.assert_awaited_once_with(CARD_ID, LIST_DONE)

    async def test_a_failed_jobs_card_goes_to_the_failure_lane(self):
        poller = _poller(STRANDED_CARD, job_for_card=_job(status=JobStatus.FAILED, error="All dev tasks failed"))

        await poller._reconcile_stranded_cards()

        poller._move_card.assert_awaited_once_with(CARD_ID, LIST_FAILED)

    async def test_the_outcome_is_commented_on_the_card(self):
        poller = _poller(STRANDED_CARD, job_for_card=_job())

        await poller._reconcile_stranded_cards()

        poller._add_comment.assert_awaited_once()

    async def test_the_card_is_not_left_in_active_afterwards(self):
        """_handle_completion owns the removal; a leak here would make the
        next poll re-process a card that is already filed."""
        poller = _poller(STRANDED_CARD, job_for_card=_job())

        await poller._reconcile_stranded_cards()

        assert CARD_ID not in poller._active

    async def test_a_card_whose_label_was_stripped_is_still_recovered(self):
        """The DB is the ownership gate, not the label — otherwise a human
        tidying labels could strand a card permanently."""
        unlabelled = [{"id": CARD_ID, "name": "stripped", "desc": "", "labels": []}]
        poller = _poller(unlabelled, job_for_card=_job())

        await poller._reconcile_stranded_cards()

        poller._move_card.assert_awaited_once_with(CARD_ID, LIST_DONE)


class TestItTouchesNothingElse:
    async def test_a_running_jobs_card_is_left_alone(self):
        poller = _poller(STRANDED_CARD, job_for_card=_job(status=JobStatus.DEV_IN_PROGRESS))

        await poller._reconcile_stranded_cards()

        poller._move_card.assert_not_awaited()

    async def test_a_card_minions_never_picked_up_is_left_alone(self):
        """Someone else's card in In progress has no job row."""
        poller = _poller(STRANDED_CARD, job_for_card=None)

        await poller._reconcile_stranded_cards()

        poller._move_card.assert_not_awaited()

    async def test_a_rehydrated_card_is_left_to_the_normal_monitor(self):
        poller = _poller(STRANDED_CARD, job_for_card=_job())
        poller._active[CARD_ID] = {"job_id": "43a3e937", "started_at": "x", "card_name": "already tracked"}

        await poller._reconcile_stranded_cards()

        poller._move_card.assert_not_awaited()
        poller.db.get_job_by_external_id.assert_not_awaited()


class TestItCannotKillTheProcess:
    async def test_an_api_failure_returns_quietly(self):
        """start() raising takes the whole process down — a stranded card is
        worth strictly less than a running poller."""
        poller = _poller(STRANDED_CARD, job_for_card=_job(), cards_raise=True)

        await poller._reconcile_stranded_cards()

        poller._move_card.assert_not_awaited()

    async def test_a_db_failure_on_one_card_does_not_stop_the_rest(self):
        good = {"id": "other-card", "name": "second", "desc": "", "labels": []}
        poller = _poller([*STRANDED_CARD, good])
        poller.db.get_job_by_external_id = AsyncMock(side_effect=[RuntimeError("db down"), _job()])

        await poller._reconcile_stranded_cards()

        poller._move_card.assert_awaited_once_with("other-card", LIST_DONE)


class TestItRunsOnStartup:
    async def test_startup_reconciles_after_rehydrating(self):
        """Order matters: rehydration fills _active with still-running jobs,
        and the reconciler skips whatever is already there."""
        import inspect

        source = inspect.getsource(TrelloPoller.start)

        assert "_reconcile_stranded_cards()" in source
        assert source.index("_rehydrate_active()") < source.index("_reconcile_stranded_cards()")
