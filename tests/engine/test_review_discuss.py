"""A discuss verdict must end in a decision, never a parked task.

The old handler bounced a discuss round to PR_OPEN via _retry_or_fail_review,
and the re-entry died on the fan-out guard: the round's DONE reviewer tasks
match the same (pr_url, revision_count), the guard returned before any verdict
handling, attempt never moved again, and the task sat IN_REVIEW forever with
only the arbiter's 15-minute in_review→pr_open oscillation for company. The
cruelest part: DISCUSS aggregates only when NOBODY objected, so one benign
"let's discuss" among approvals wedged the whole job.

The fix mirrors the silent-reviewer re-run: the discussers get ONE more pass,
told to commit; persistent indecision fails closed into the bounded revision
path. These tests pin all three legs and the wedge regression itself.
"""

from unittest.mock import patch

from minions.core.models import AgentRole, Task, TaskStatus
from tests.engine.test_review_fanout import _engine, _provider


async def _engineer_task(db, job):
    task = await db.create_task(
        Task(
            job_id=job.id,
            title="Add a thing",
            service="wallet-api",
            agent_role=AgentRole.BACKEND_ENGINEER,
            status=TaskStatus.PR_OPEN,
            branch_name="feat/x",
            pr_number=23,
            pr_url="https://github.com/flippin-balls/wallet-api/pull/23",
            mr_id="23",
        )
    )
    await db.update_task(task.id, status=TaskStatus.IN_REVIEW)
    return await db.get_task(task.id)


def _stateful_verdicts(sequences: dict[str, list[str]], default: str = "approve"):
    """run_agent stub whose verdict advances through a per-specialty sequence.

    A specialty not in `sequences` always answers `default`. A specialty whose
    sequence is exhausted repeats its last entry — so {"api": ["discuss"]}
    models a reviewer that will not commit no matter how often it is asked.
    """
    calls: list[str] = []
    seen: dict[str, int] = {}

    async def _run(**kwargs):
        specialty = kwargs["task"].specialty
        calls.append(specialty)
        n = seen.get(specialty, 0)
        seen[specialty] = n + 1
        seq = sequences.get(specialty)
        if seq is None:
            verdict = default
        else:
            verdict = seq[min(n, len(seq) - 1)]
        result = kwargs["agent"]
        result.status = "done"
        result._review_verdict = verdict
        return result

    return _run, calls


async def _run_review(db, job, task, run):
    engine = _engine(db)
    with (
        patch("minions.engine.dev.run_agent", new=run),
        patch("minions.engine.review._create_provider_for_project", return_value=_provider(["app/service.py"])),
        patch("minions.repos.ensure_checkout", return_value=True),
    ):
        from minions.engine.dev import run_task_review

        await run_task_review(engine, job, task)
    return engine


async def _events(db, job, event_type):
    return [e for e in await db.get_events(job.id) if e.get("event_type") == event_type]


class TestDiscussIsReAskedOnce:
    async def test_a_discusser_who_commits_on_the_re_ask_lets_the_pr_through(self, db, sample_job):
        task = await _engineer_task(db, sample_job)
        run, calls = _stateful_verdicts({"backend-architecture": ["discuss", "approve"]})

        await _run_review(db, sample_job, task, run)

        assert calls.count("backend-architecture") == 2, "the discusser gets exactly one more run"
        assert calls.count("api") == 1, "reviewers who already decided are not re-run"
        assert (await db.get_task(task.id)).status == TaskStatus.MERGED
        assert await _events(db, sample_job, "review_discuss_rerun")

    async def test_a_discusser_who_blocks_on_the_re_ask_routes_to_revision(self, db, sample_job):
        task = await _engineer_task(db, sample_job)
        run, _ = _stateful_verdicts({"backend-architecture": ["discuss", "request_changes"]})

        await _run_review(db, sample_job, task, run)

        refreshed = await db.get_task(task.id)
        assert refreshed.status == TaskStatus.IN_PROGRESS
        assert (refreshed.review_status or "").startswith("changes_requested")


class TestPersistentDiscussFailsClosed:
    async def test_the_task_lands_in_revision_not_in_review(self, db, sample_job):
        """The wedge regression itself: the old path left the task at
        IN_REVIEW with a bumped attempt and nothing left to fire."""
        task = await _engineer_task(db, sample_job)
        run, calls = _stateful_verdicts({"backend-architecture": ["discuss"]})

        await _run_review(db, sample_job, task, run)

        refreshed = await db.get_task(task.id)
        assert refreshed.status == TaskStatus.IN_PROGRESS, "never parked at IN_REVIEW, never bounced to PR_OPEN"
        assert (refreshed.review_status or "").startswith("changes_requested")
        assert "discussion unresolved" in (refreshed.review_status or "")
        assert refreshed.attempt == task.attempt, "review attempts are not consumed by indecision"
        assert calls.count("backend-architecture") == 2
        assert await _events(db, sample_job, "review_discuss_failclosed")


class TestDiscussDefersToRealSignals:
    async def test_an_objection_in_the_round_skips_the_re_ask(self, db, sample_job):
        """Someone requested changes: a revision is happening regardless, and
        the discussion points ride along in the review text."""
        task = await _engineer_task(db, sample_job)
        run, calls = _stateful_verdicts({"backend-architecture": ["discuss"], "api": ["request_changes"]})

        await _run_review(db, sample_job, task, run)

        assert calls.count("backend-architecture") == 1, "no re-ask when the round already has its answer"
        refreshed = await db.get_task(task.id)
        assert refreshed.status == TaskStatus.IN_PROGRESS
        assert not await _events(db, sample_job, "review_discuss_rerun")
