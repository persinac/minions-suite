"""A re-queued card has TWO jobs; the lookup must return the live one.

Cards get re-queued for good reasons — card 86joZOFr was misrouted to a repo
with no Playwright, closed no_work_needed, then re-labelled with an explicit
destination. That leaves two jobs sharing one external_id.

Unordered, `SELECT * FROM jobs WHERE external_id = ?` returns an arbitrary row
— in practice the oldest. That is not cosmetic: _reconcile_stranded_cards
(providers/trello.py) looks a card's job up by external_id and files the card
to Done when it finds a terminal one. Given the stale row it would file a card
whose NEW job is still running, killing the live job's card mid-flight.
"""

from minions.core.models import JobStatus


async def _job_for_card(db, card_id: str, spec: str):
    return await db.create_job(spec, external_id=card_id)


async def _close_no_work_needed(db, job_id: str):
    """Walk the legal path — no_work_needed is only reachable once a job has
    been decomposed, because it is a conclusion reached by READING the code."""
    for status in (JobStatus.SPEC_READY, JobStatus.TASKS_CREATED, JobStatus.NO_WORK_NEEDED):
        await db.update_job_status(job_id, status)


class TestNewestJobWins:
    async def test_a_requeued_card_resolves_to_the_new_job(self, db):
        card = "6a8956023d62b81cc448087d"
        old = await _job_for_card(db, card, "first attempt - misrouted")
        await _close_no_work_needed(db, old.id)
        new = await _job_for_card(db, card, "re-queued with an explicit destination")

        found = await db.get_job_by_external_id(card)

        assert found is not None
        assert found.id == new.id, "the LIVE job must win, not the terminal one it replaced"

    async def test_the_live_jobs_status_is_what_the_reconciler_sees(self, db):
        """The consequence that matters: a stale terminal row would make
        _reconcile_stranded_cards file a card whose job is still running."""
        card = "card-with-history"
        old = await _job_for_card(db, card, "old")
        await _close_no_work_needed(db, old.id)
        await _job_for_card(db, card, "new")

        found = await db.get_job_by_external_id(card)

        assert found.status not in (JobStatus.DONE, JobStatus.FAILED, JobStatus.NO_WORK_NEEDED)

    async def test_a_single_job_card_is_unchanged(self, db):
        card = "card-with-one-job"
        only = await _job_for_card(db, card, "solo")

        found = await db.get_job_by_external_id(card)

        assert found.id == only.id

    async def test_an_unknown_card_is_still_none(self, db):
        assert await db.get_job_by_external_id("no-such-card") is None
