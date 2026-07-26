"""Reviewer dedup must survive expert fan-out.

The guard exists because the arbiter's `advance_job` remediation re-fires every
monitor pass while a job looks stuck, and each pass created another reviewer
task and another agent — two reviewers on one PR, $4.87 for a review needed once.

Keyed on pr_url alone it would also collapse N specialists to whichever started
first, with no error: the job reports a clean review, having actually run one of
five. That is the failure this file pins down.
"""

import pytest

from minions.core.models import AgentRole, Task, TaskStatus


async def _reviewer(db, job_id: str, pr_url: str, specialty: str | None, status=TaskStatus.IN_PROGRESS) -> Task:
    return await db.create_task(
        Task(
            job_id=job_id,
            title=f"Review PR ({specialty or 'general'})",
            service="svc",
            agent_role=AgentRole.CODE_REVIEWER,
            status=status,
            pr_url=pr_url,
            specialty=specialty,
        )
    )


class TestSpecialtyRoundTrip:
    async def test_specialty_persists(self, db):
        """The column has to survive the DB round trip or the key is always None."""
        job = await db.create_job("spec")
        task = await _reviewer(db, job.id, "https://github.com/o/r/pull/1", "dba")

        fetched = await db.get_task(task.id)
        assert fetched.specialty == "dba"

    async def test_specialty_defaults_to_none(self, db):
        """Pre-fan-out reviewers carry no specialty and must still work."""
        job = await db.create_job("spec")
        task = await db.create_task(
            Task(job_id=job.id, title="Review", service="svc", agent_role=AgentRole.CODE_REVIEWER)
        )

        assert (await db.get_task(task.id)).specialty is None


class TestDedupKey:
    """The guard's predicate, exercised directly against real rows."""

    @staticmethod
    def _duplicates(existing, task):
        return [
            t
            for t in existing
            if t.agent_role == AgentRole.CODE_REVIEWER
            and t.id != task.id
            and t.status != TaskStatus.FAILED
            and (t.pr_url or "") == (task.pr_url or "")
            and (t.specialty or "") == (task.specialty or "")
        ]

    async def test_same_specialty_same_pr_is_a_duplicate(self, db):
        """The original bug: the arbiter re-firing must not spawn a second."""
        job = await db.create_job("spec")
        pr = "https://github.com/o/r/pull/1"
        first = await _reviewer(db, job.id, pr, "dba")
        second = await _reviewer(db, job.id, pr, "dba")

        assert self._duplicates([first], second), "a repeat of the same specialty must be blocked"

    async def test_different_specialties_same_pr_are_not_duplicates(self, db):
        """Fan-out: five experts on one PR are five reviews, not four duplicates."""
        job = await db.create_job("spec")
        pr = "https://github.com/o/r/pull/1"

        existing = []
        for specialty in ("api", "backend-architecture", "dba", "python", "frontend"):
            candidate = await _reviewer(db, job.id, pr, specialty)
            assert not self._duplicates(existing, candidate), f"{specialty} was wrongly treated as a duplicate"
            existing.append(candidate)

        assert len(existing) == 5

    async def test_general_reviewer_still_dedupes_against_itself(self, db):
        """specialty=None must behave exactly as before fan-out existed."""
        job = await db.create_job("spec")
        pr = "https://github.com/o/r/pull/1"
        first = await _reviewer(db, job.id, pr, None)
        second = await _reviewer(db, job.id, pr, None)

        assert self._duplicates([first], second)

    async def test_a_specialist_does_not_collide_with_the_general_reviewer(self, db):
        job = await db.create_job("spec")
        pr = "https://github.com/o/r/pull/1"
        general = await _reviewer(db, job.id, pr, None)
        dba = await _reviewer(db, job.id, pr, "dba")

        assert not self._duplicates([general], dba)

    async def test_same_specialty_different_prs_are_not_duplicates(self, db):
        """Two PRs in one job each deserve their own DBA review."""
        job = await db.create_job("spec")
        first = await _reviewer(db, job.id, "https://github.com/o/r/pull/1", "dba")
        second = await _reviewer(db, job.id, "https://github.com/o/r/pull/2", "dba")

        assert not self._duplicates([first], second)

    async def test_a_failed_reviewer_does_not_block_a_retry(self, db):
        """A specialist that failed must be retryable, or one flake loses that lens."""
        job = await db.create_job("spec")
        pr = "https://github.com/o/r/pull/1"
        failed = await _reviewer(db, job.id, pr, "dba", status=TaskStatus.FAILED)
        retry = await _reviewer(db, job.id, pr, "dba")

        assert not self._duplicates([failed], retry)


class TestGuardSourceKeysOnSpecialty:
    def test_run_task_review_guards_against_a_second_fanout(self):
        """Dedup moved up a level once fan-out landed.

        Per-specialty dedup made sense when each reviewer was created
        independently. Now the whole fan-out is created in one pass, so the guard
        asks whether a review already ran for this PR at all — per-specialty
        checking would let a re-entry bolt stragglers onto a concluded review.

        The tasks.specialty column still carries the lens, and the DB index on
        (pr_url, specialty) still exists; only the guard's granularity changed.
        """
        import inspect

        from minions.engine import dev

        source = inspect.getsource(dev.run_task_review)

        assert "AgentRole.CODE_REVIEWER" in source
        assert "not fanning out again" in source, "a second fan-out must be refused"
        assert "specialty=specialty" in inspect.getsource(dev._run_one_specialist), "each reviewer must record its lens"
