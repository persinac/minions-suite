"""Compare-and-swap semantics on update_job_status.

Two JobEngines polling the same database will both try to advance the same job.
Without a guard each dispatches its own agents — two engineers pushing branches
for the same task. ENGINE_ENABLED is the primary defence (one engine per
deployment); `expected_status` is the backstop when that is broken by hand.
"""

import pytest

from minions.core.models import Job, JobStatus


# The state machine only permits single steps (JOB_TRANSITIONS), so a fixture
# cannot jump straight to an arbitrary status — it has to walk the happy path.
_PATH_TO: dict[JobStatus, list[JobStatus]] = {
    JobStatus.SPEC_RECEIVED: [],
    JobStatus.SPEC_READY: [JobStatus.SPEC_READY],
    JobStatus.TASKS_CREATED: [JobStatus.SPEC_READY, JobStatus.TASKS_CREATED],
    JobStatus.DEV_IN_PROGRESS: [
        JobStatus.SPEC_READY,
        JobStatus.TASKS_CREATED,
        JobStatus.DEV_IN_PROGRESS,
    ],
}


async def _make_job(db, status: JobStatus) -> Job:
    """Create a job and walk it to `status` one legal transition at a time."""
    job = await db.create_job("test spec")
    for step in _PATH_TO[status]:
        await db.update_job_status(job.id, step)
    return await db.get_job(job.id)


class TestUpdateJobStatusCas:
    async def test_blind_write_still_returns_true(self, db):
        """No expected_status -> historical behaviour, unconditional write."""
        job = await _make_job(db, JobStatus.SPEC_RECEIVED)

        won = await db.update_job_status(job.id, JobStatus.SPEC_READY)

        assert won is True
        assert (await db.get_job(job.id)).status == JobStatus.SPEC_READY

    async def test_cas_succeeds_when_status_matches(self, db):
        job = await _make_job(db, JobStatus.TASKS_CREATED)

        won = await db.update_job_status(
            job.id, JobStatus.DEV_IN_PROGRESS, expected_status=JobStatus.TASKS_CREATED
        )

        assert won is True
        assert (await db.get_job(job.id)).status == JobStatus.DEV_IN_PROGRESS

    async def test_cas_loses_when_another_process_already_advanced(self, db):
        """The race: engine A advances, engine B's CAS must not write."""
        job = await _make_job(db, JobStatus.TASKS_CREATED)

        # Engine A wins.
        first = await db.update_job_status(
            job.id, JobStatus.DEV_IN_PROGRESS, expected_status=JobStatus.TASKS_CREATED
        )
        # Engine B, still holding a stale read of TASKS_CREATED, loses.
        second = await db.update_job_status(
            job.id, JobStatus.DEV_IN_PROGRESS, expected_status=JobStatus.TASKS_CREATED
        )

        assert first is True
        assert second is False
        assert (await db.get_job(job.id)).status == JobStatus.DEV_IN_PROGRESS

    async def test_lost_cas_does_not_clobber_a_later_status(self, db):
        """A slow loser must not drag the job back to an earlier state."""
        job = await _make_job(db, JobStatus.TASKS_CREATED)
        await db.update_job_status(
            job.id, JobStatus.DEV_IN_PROGRESS, expected_status=JobStatus.TASKS_CREATED
        )
        await db.update_job_status(job.id, JobStatus.PR_OPEN)

        won = await db.update_job_status(
            job.id, JobStatus.DEV_IN_PROGRESS, expected_status=JobStatus.TASKS_CREATED
        )

        assert won is False
        assert (await db.get_job(job.id)).status == JobStatus.PR_OPEN

    async def test_terminal_transition_lands_regardless(self, db):
        """FAILED is deliberately a blind write — it must not depend on a CAS."""
        job = await _make_job(db, JobStatus.DEV_IN_PROGRESS)

        won = await db.update_job_status(job.id, JobStatus.FAILED, error="boom")

        assert won is True
        refreshed = await db.get_job(job.id)
        assert refreshed.status == JobStatus.FAILED
        assert "boom" in (refreshed.error or "")
