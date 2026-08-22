"""Specialist fan-out, driven end to end.

The other reviewer tests check pieces in isolation — which specialists fire, how
verdicts aggregate, that the guard exists. These drive run_task_review itself,
because the failure that matters is the wiring: N tasks created, N agents run,
verdicts collected and collapsed, and the merge decision taken from the result.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from minions.core.models import Agent, AgentRole, Task, TaskStatus


def _engine(db, auto_merge=False, fanout_max=0):
    engine = MagicMock()
    engine.db = db
    engine.config = MagicMock()
    engine.config.model = "test-model"
    engine.config.job_cost_limit_usd = 1000.0
    engine.config.agent_cost_limit_usd = 100.0
    engine.config.require_ci_pass = False
    # Uncapped by default so the tests below keep measuring the *wiring* --
    # inference to tasks to verdicts -- independently of how wide the panel is
    # allowed to be. TestFanoutCap covers the cap itself, at the real default.
    engine.config.review_fanout_max = fanout_max
    engine.config.model_reviewer = "test-reviewer"
    engine.config.model_easy = "e"
    engine.config.model_medium = "m"
    engine.config.model_hard = "h"
    engine._k8s_enabled = False
    engine._nats_agent_status = AsyncMock()
    engine._trello_comment = AsyncMock()
    engine._maybe_dry_run = MagicMock(side_effect=lambda x: x)

    project = MagicMock()
    project.model = ""
    project.project_id = "flippin-balls/wallet-api"
    project.auto_merge = auto_merge
    project.git_provider = "gitlab"  # avoids the reviewer-App path
    service = MagicMock(repo_path="/tmp", default_branch="main")
    engine._resolve_service.return_value = (project, service)
    return engine


async def _engineer_task(db, job, files_hint="app/service.py"):
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


def _provider(changed_files, diff=""):
    provider = AsyncMock()
    provider.get_changed_files.return_value = changed_files
    provider.get_diff.return_value = diff
    return provider


def _verdicts(mapping):
    """run_agent stub returning a per-specialty verdict, keyed by task title."""
    calls = []

    async def _run(**kwargs):
        task = kwargs["task"]
        calls.append(task.specialty)
        result = kwargs["agent"]
        result.status = "done"
        result._review_verdict = mapping.get(task.specialty, "approve")
        return result

    return _run, calls


class TestFanOut:
    async def test_creates_one_task_per_specialist(self, db, sample_job):
        task = await _engineer_task(db, sample_job)
        engine = _engine(db)
        run, calls = _verdicts({})

        with (
            patch("minions.engine.dev.run_agent", new=run),
            patch("minions.engine.review._create_provider_for_project", return_value=_provider(["app/service.py"])),
        ):
            from minions.engine.dev import run_task_review

            await run_task_review(engine, sample_job, task)

        reviewer_tasks = [t for t in await db.get_tasks(sample_job.id) if t.agent_role == AgentRole.CODE_REVIEWER]
        specialties = {t.specialty for t in reviewer_tasks}

        # .py wakes pythonista on top of the two always-on reviewers.
        assert specialties == {"api", "backend-architecture", "pythonista"}
        assert len(calls) == 3, "every specialist must actually run"

    async def test_dba_wakes_on_diff_content_not_paths(self, db, sample_job):
        """The whole reason the trigger reads the diff."""
        task = await _engineer_task(db, sample_job)
        engine = _engine(db)
        run, calls = _verdicts({})
        diff = "+    rows = session.query(Play).filter_by(user_id=uid).all()\n"

        with (
            patch("minions.engine.dev.run_agent", new=run),
            patch("minions.engine.review._create_provider_for_project", return_value=_provider(["app/service.py"], diff)),
        ):
            from minions.engine.dev import run_task_review

            await run_task_review(engine, sample_job, task)

        assert "dba" in set(calls), "an ORM query in ordinary code must wake the DBA"

    async def test_a_second_fanout_is_refused(self, db, sample_job):
        """The arbiter's advance_job remediation re-fires while a job looks stuck."""
        task = await _engineer_task(db, sample_job)
        engine = _engine(db)
        run, calls = _verdicts({})

        with (
            patch("minions.engine.dev.run_agent", new=run),
            patch("minions.engine.review._create_provider_for_project", return_value=_provider(["app/service.py"])),
        ):
            from minions.engine.dev import run_task_review

            await run_task_review(engine, sample_job, task)
            first = len(calls)
            await run_task_review(engine, sample_job, task)

        assert len(calls) == first, "the second pass must not run any reviewer again"


class TestVerdictDrivesOutcome:
    async def test_all_approve_merges(self, db, sample_job):
        task = await _engineer_task(db, sample_job)
        engine = _engine(db)
        run, _ = _verdicts({})

        with (
            patch("minions.engine.dev.run_agent", new=run),
            patch("minions.engine.review._create_provider_for_project", return_value=_provider(["app/service.py"])),
        ):
            from minions.engine.dev import run_task_review

            await run_task_review(engine, sample_job, task)

        assert (await db.get_task(task.id)).status == TaskStatus.MERGED

    async def test_one_blocker_sends_it_back_for_revision(self, db, sample_job):
        """A single specialist objecting outranks the others approving."""
        task = await _engineer_task(db, sample_job)
        engine = _engine(db)
        run, _ = _verdicts({"pythonista": "request_changes"})

        with (
            patch("minions.engine.dev.run_agent", new=run),
            patch("minions.engine.review._create_provider_for_project", return_value=_provider(["app/service.py"])),
        ):
            from minions.engine.dev import run_task_review

            await run_task_review(engine, sample_job, task)

        updated = await db.get_task(task.id)
        assert updated.status == TaskStatus.IN_PROGRESS
        assert "pythonista" in (updated.review_status or "")

    async def test_a_silent_specialist_does_not_approve(self, db, sample_job):
        """One reviewer producing no verdict must block the merge, not be ignored."""
        task = await _engineer_task(db, sample_job)
        engine = _engine(db)
        run, _ = _verdicts({"api": None})

        with (
            patch("minions.engine.dev.run_agent", new=run),
            patch("minions.engine.review._create_provider_for_project", return_value=_provider(["app/service.py"])),
        ):
            from minions.engine.dev import run_task_review

            await run_task_review(engine, sample_job, task)

        assert (await db.get_task(task.id)).status != TaskStatus.MERGED

    async def test_a_crashing_specialist_does_not_take_down_the_others(self, db, sample_job):
        """One reviewer raising must not lose the reviews that succeeded."""
        task = await _engineer_task(db, sample_job)
        engine = _engine(db)
        seen = []

        async def _run(**kwargs):
            specialty = kwargs["task"].specialty
            seen.append(specialty)
            if specialty == "api":
                raise RuntimeError("boom")
            result = kwargs["agent"]
            result.status = "done"
            result._review_verdict = "approve"
            return result

        with (
            patch("minions.engine.dev.run_agent", new=_run),
            patch("minions.engine.review._create_provider_for_project", return_value=_provider(["app/service.py"])),
        ):
            from minions.engine.dev import run_task_review

            await run_task_review(engine, sample_job, task)

        assert set(seen) == {"api", "backend-architecture", "pythonista"}, "the other specialists must still have run"
        assert seen.count("backend-architecture") == 1
        assert seen.count("pythonista") == 1
        # A crash reads as silence next to two approvals, so api is re-run once.
        assert seen.count("api") == 2
        # It crashed both times — the aggregate fails closed.
        assert (await db.get_task(task.id)).status != TaskStatus.MERGED


class TestSpendCeiling:
    async def test_an_over_budget_job_does_not_fan_out(self, db, sample_job):
        """Reviewers call run_agent directly, bypassing _run_in_process — so the
        job ceiling has to be re-checked here or it never applies to them."""
        task = await _engineer_task(db, sample_job)
        engine = _engine(db)
        engine.config.job_cost_limit_usd = 5.0

        agent = await db.create_agent(Agent(job_id=sample_job.id, role=AgentRole.BACKEND_ENGINEER, task_id=task.id, model="m"))
        await db.update_agent(agent.id, cost_usd=9.0)

        run, calls = _verdicts({})
        with (
            patch("minions.engine.dev.run_agent", new=run),
            patch("minions.engine.review._create_provider_for_project", return_value=_provider(["app/service.py"])),
        ):
            from minions.engine.dev import run_task_review

            await run_task_review(engine, sample_job, task)

        assert calls == [], "no reviewer may run once the job is over budget"
        assert (await db.get_task(task.id)).status != TaskStatus.MERGED


class TestFanoutCap:
    """The cap, wired end to end at its real production default of 2.

    `TestFanOut` above runs uncapped on purpose so it measures the wiring. This
    class measures the narrowing itself: that fewer agents actually run, that
    the survivors are the ones the diff asked for, and that the drop leaves a
    trace. The last one carries the most weight — a reviewer that never ran
    cannot report what it would have found, so `capped=` in the audit event is
    the only evidence the panel was smaller than the diff called for.
    """

    async def test_cap_reduces_the_agents_that_actually_run(self, db, sample_job):
        task = await _engineer_task(db, sample_job)
        engine = _engine(db, fanout_max=2)
        run, calls = _verdicts({})

        with (
            patch("minions.engine.dev.run_agent", new=run),
            patch("minions.engine.review._create_provider_for_project", return_value=_provider(["app/service.py"])),
        ):
            from minions.engine.dev import run_task_review

            await run_task_review(engine, sample_job, task)

        # Uncapped this PR wakes three (api, backend-architecture, pythonista).
        assert len(calls) == 2, f"cap did not bind: {calls}"
        assert set(calls) == {"backend-architecture", "pythonista"}
        assert "api" not in calls, "the unconditional generalist should yield to the fired specialist"

    async def test_content_triggered_dba_survives_the_cap_end_to_end(self, db, sample_job):
        """The DBA content trigger must not become dead code at the default width."""
        task = await _engineer_task(db, sample_job)
        engine = _engine(db, fanout_max=2)
        run, calls = _verdicts({})
        diff = "+    rows = session.query(Play).filter_by(user_id=uid).all()\n"

        with (
            patch("minions.engine.dev.run_agent", new=run),
            patch("minions.engine.review._create_provider_for_project", return_value=_provider(["db/migrations/1.sql"], diff)),
        ):
            from minions.engine.dev import run_task_review

            await run_task_review(engine, sample_job, task)

        assert "dba" in set(calls), "a SQL migration must still wake the DBA at cap=2"

    async def test_the_drop_is_recorded_in_the_audit_event(self, db, sample_job):
        """`capped=` is the only trace a narrowed gate leaves behind."""
        task = await _engineer_task(db, sample_job)
        engine = _engine(db, fanout_max=2)
        run, _calls = _verdicts({})

        with (
            patch("minions.engine.dev.run_agent", new=run),
            patch("minions.engine.review._create_provider_for_project", return_value=_provider(["app/service.py"])),
        ):
            from minions.engine.dev import run_task_review

            await run_task_review(engine, sample_job, task)

        events = [e for e in await db.get_events(sample_job.id) if e["event_type"] == "review_fanout"]
        assert events, "fan-out must record an audit event"
        detail = events[0]["detail"]

        assert "ran=backend-architecture,pythonista" in detail
        assert "capped=api" in detail, f"the dropped reviewer must be named: {detail}"

    async def test_uncapped_run_records_an_empty_capped_field(self, db, sample_job):
        """No cap, no drop — the field stays present but empty, so it is greppable."""
        task = await _engineer_task(db, sample_job)
        engine = _engine(db, fanout_max=0)
        run, _calls = _verdicts({})

        with (
            patch("minions.engine.dev.run_agent", new=run),
            patch("minions.engine.review._create_provider_for_project", return_value=_provider(["app/service.py"])),
        ):
            from minions.engine.dev import run_task_review

            await run_task_review(engine, sample_job, task)

        events = [e for e in await db.get_events(sample_job.id) if e["event_type"] == "review_fanout"]
        assert "capped=" in events[0]["detail"]
        assert "capped=api" not in events[0]["detail"]


class TestSilenceIsRerunNotRevised:
    """A reviewer that never answered is run again, not laundered into a revision.

    Jobs 33c89d9b and 2b63f1b6: two approvals plus one silent reviewer forced a
    revision nobody had asked for — and 33c89d9b's second round then flipped the
    verdict on byte-identical code. Silence needs the reviewer run again; only
    an objection needs the engineer. missing_verdicts (tests/test_missing_verdicts.py)
    is the discriminator; these tests pin the wiring.
    """

    @staticmethod
    def _scripted(script):
        """run_agent stub reading each specialist's verdict off a per-call list;
        specialists not named always approve, and the last entry repeats."""
        calls = []

        async def _run(**kwargs):
            specialty = kwargs["task"].specialty
            nth = calls.count(specialty)
            calls.append(specialty)
            result = kwargs["agent"]
            result.status = "done"
            per_call = script.get(specialty)
            if per_call is None:
                result._review_verdict = "approve"
            else:
                result._review_verdict = per_call[min(nth, len(per_call) - 1)]
            return result

        return _run, calls

    async def _review(self, db, job, task, run):
        engine = _engine(db)
        with (
            patch("minions.engine.dev.run_agent", new=run),
            patch("minions.engine.review._create_provider_for_project", return_value=_provider(["app/service.py"])),
        ):
            from minions.engine.dev import run_task_review

            await run_task_review(engine, job, task)
        return engine

    async def test_a_silent_reviewer_that_answers_on_the_rerun_unblocks_the_pr(self, db, sample_job):
        """The headline: no revision round for a PR nobody objected to."""
        task = await _engineer_task(db, sample_job)
        run, calls = self._scripted({"api": [None, "approve"]})

        await self._review(db, sample_job, task, run)

        assert calls.count("api") == 2, "the silent reviewer must be run again"
        updated = await db.get_task(task.id)
        assert updated.status == TaskStatus.MERGED, "with the re-run approval the panel is unanimous"
        assert not (updated.review_status or "").startswith("changes_requested")

    async def test_a_rerun_that_stays_silent_still_fails_closed(self, db, sample_job):
        """The backstop is untouched: double silence never counts as assent."""
        task = await _engineer_task(db, sample_job)
        run, calls = self._scripted({"api": [None, None]})

        await self._review(db, sample_job, task, run)

        assert calls.count("api") == 2, "exactly one re-run — silence must not retry forever"
        updated = await db.get_task(task.id)
        assert updated.status == TaskStatus.IN_PROGRESS
        assert "no usable verdict" in (updated.review_status or "")

    async def test_an_objection_alongside_silence_goes_straight_to_revision(self, db, sample_job):
        """The safety property, wired: a genuine block always goes to revision,
        and no money is spent re-asking a reviewer the revision will re-run anyway."""
        task = await _engineer_task(db, sample_job)
        run, calls = self._scripted({"api": [None], "pythonista": ["request_changes"]})

        await self._review(db, sample_job, task, run)

        assert calls.count("api") == 1, "silence next to an objection must not be re-run"
        assert (await db.get_task(task.id)).status == TaskStatus.IN_PROGRESS

    async def test_the_rerun_leaves_an_audit_event(self, db, sample_job):
        """Named, so a reviewer that goes silent repeatedly shows up in the events."""
        task = await _engineer_task(db, sample_job)
        run, _calls = self._scripted({"api": [None, "approve"]})

        await self._review(db, sample_job, task, run)

        events = [e for e in await db.get_events(sample_job.id) if e["event_type"] == "review_silent_rerun"]
        assert len(events) == 1
        assert "rerun=api" in events[0]["detail"]

    async def test_no_rerun_events_when_everyone_answered(self, db, sample_job):
        task = await _engineer_task(db, sample_job)
        run, _calls = self._scripted({})

        await self._review(db, sample_job, task, run)

        events = [e for e in await db.get_events(sample_job.id) if e["event_type"] == "review_silent_rerun"]
        assert events == []

    async def test_a_job_that_went_over_budget_mid_panel_does_not_rerun(self, db, sample_job):
        """The entry ceiling check ran before the panel spent anything; the
        re-run needs its own, or silence becomes a way to spend past the cap."""
        task = await _engineer_task(db, sample_job)
        calls = []

        async def _run(**kwargs):
            specialty = kwargs["task"].specialty
            calls.append(specialty)
            result = kwargs["agent"]
            result.status = "done"
            if specialty == "api":
                # The panel itself pushes the job over its limit, then goes silent.
                await kwargs["db"].update_agent(result.id, cost_usd=9.0)
                result._review_verdict = None
            else:
                result._review_verdict = "approve"
            return result

        engine = None
        with (
            patch("minions.engine.dev.run_agent", new=_run),
            patch("minions.engine.review._create_provider_for_project", return_value=_provider(["app/service.py"])),
        ):
            from minions.engine.dev import run_task_review

            engine = _engine(db)
            engine.config.job_cost_limit_usd = 5.0
            await run_task_review(engine, sample_job, task)

        assert calls.count("api") == 1, "no reviewer may be re-run once the job is over budget"
        updated = await db.get_task(task.id)
        assert updated.status == TaskStatus.IN_PROGRESS, "over budget degrades to the fail-closed aggregate"
        events = [e for e in await db.get_events(sample_job.id) if e["event_type"] == "job_cost_limit_exceeded"]
        assert any("not re-running silent reviewers" in e["detail"] for e in events)
