"""A missing review verdict must never count as approval.

`approved = verdict == "approve" or (verdict is None)` meant any reviewer that
crashed, timed out, hit the cost ceiling, or never called submit_review was
treated as having approved.

Not hypothetical: on job f6451f44 submit_review raised — GitHub refuses a
self-authored review — `_review_verdict` was never set, and the task advanced to
MERGED with no review recorded anywhere. auto_merge was off at the time. It is
now on for every project, so that same path merges to main.

Found by the ui-integration-tests agent while scoping the CI gate.
"""

import pytest

from minions.core.models import AgentRole, Task, TaskStatus


class TestMissingVerdictFailsClosed:
    def test_the_or_none_approval_is_gone(self):
        """The exact expression that caused it, pinned so it cannot come back.

        With fan-out the decision moved into aggregate_verdicts, which fails
        closed on a missing verdict — see tests/test_reviewers.py. What matters
        here is that run_task_review routes through it rather than reintroducing
        an inline truthiness check.
        """
        import inspect

        from minions.engine import dev

        source = inspect.getsource(dev.run_task_review)

        assert 'verdict == "approve" or (verdict is None)' not in source
        assert "aggregate_verdicts" in source, "the verdict decision must go through the fail-closed aggregator"

    def test_merge_is_unreachable_without_an_explicit_approval(self):
        """Anything short of approval must return before the merge call.

        aggregate_verdicts collapses a missing verdict to request_changes, and
        that branch — plus the discuss branch — must exit ahead of merge_mr.
        """
        import inspect

        from minions.engine import dev

        source = inspect.getsource(dev.run_task_review)
        aggregated = source.index("aggregate_verdicts(verdicts)")
        blocked = source.index('if verdict == "request_changes":')
        discuss = source.index('if verdict == "discuss":')
        merge_call = source.index("merge_provider.merge_mr")

        assert aggregated < blocked < discuss < merge_call, "non-approval branches must precede the merge"
        # Both guards have to leave the function, not merely log.
        assert source[blocked:discuss].count("return") >= 1
        assert source[discuss:merge_call].count("return") >= 1


class TestRetryOrFail:
    async def test_retries_while_attempts_remain(self, db):
        from minions.engine.dev import _retry_or_fail_review

        job = await db.create_job("spec")
        task = await db.create_task(
            Task(
                job_id=job.id,
                title="t",
                service="svc",
                agent_role=AgentRole.BACKEND_ENGINEER,
                status=TaskStatus.PR_OPEN,
                attempt=1,
                max_attempts=3,
            )
        )

        engine = type("E", (), {"db": db})()
        await _retry_or_fail_review(engine, task, "no verdict")

        updated = await db.get_task(task.id)
        assert updated.status == TaskStatus.PR_OPEN, "should go back for another review, not forward"
        assert updated.attempt == 2

    async def test_fails_when_attempts_are_exhausted(self, db):
        """Exhausted retries must stop, not fall through to approval."""
        from minions.engine.dev import _retry_or_fail_review

        job = await db.create_job("spec")
        task = await db.create_task(
            Task(
                job_id=job.id,
                title="t",
                service="svc",
                agent_role=AgentRole.BACKEND_ENGINEER,
                status=TaskStatus.PR_OPEN,
                attempt=3,
                max_attempts=3,
            )
        )

        engine = type("E", (), {"db": db})()
        await _retry_or_fail_review(engine, task, "no verdict")

        updated = await db.get_task(task.id)
        assert updated.status == TaskStatus.FAILED
        assert "no review attempts left" in (updated.error or "")

    async def test_never_reaches_a_merge_ready_state(self, db):
        """Whatever happens, the task must not land anywhere that implies approval."""
        from minions.engine.dev import _retry_or_fail_review

        job = await db.create_job("spec")
        engine = type("E", (), {"db": db})()

        for attempt in (1, 3):
            task = await db.create_task(
                Task(
                    job_id=job.id,
                    title=f"t{attempt}",
                    service="svc",
                    agent_role=AgentRole.BACKEND_ENGINEER,
                    status=TaskStatus.PR_OPEN,
                    attempt=attempt,
                    max_attempts=3,
                )
            )
            await _retry_or_fail_review(engine, task, "no verdict")

            status = (await db.get_task(task.id)).status
            assert status not in (TaskStatus.MERGED, TaskStatus.DONE), f"attempt {attempt} reached {status}"


class TestRetryFromInReview:
    """The real path: the task is in_review when the reviewer finishes."""

    async def test_in_review_returns_to_pr_open_and_increments(self, db):
        from minions.engine.dev import _retry_or_fail_review

        job = await db.create_job("spec")
        task = await db.create_task(
            Task(
                job_id=job.id,
                title="t",
                service="svc",
                agent_role=AgentRole.BACKEND_ENGINEER,
                status=TaskStatus.PR_OPEN,
                attempt=1,
                max_attempts=3,
                # A task at review time has opened a PR; pr_open has these as
                # required preconditions, so omitting them tests a state that
                # cannot occur.
                branch_name="feat/x",
                pr_number=23,
                pr_url="https://github.com/o/r/pull/23",
            )
        )
        await db.update_task(task.id, status=TaskStatus.IN_REVIEW)
        task = await db.get_task(task.id)

        engine = type("E", (), {"db": db})()
        await _retry_or_fail_review(engine, task, "no verdict")

        updated = await db.get_task(task.id)
        assert updated.status == TaskStatus.PR_OPEN, "in_review -> pr_open is the legal retry path"
        assert updated.attempt == 2, "the attempt counter must actually move, or retries loop forever"
