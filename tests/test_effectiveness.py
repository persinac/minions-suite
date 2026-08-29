"""Cost joined to quality — the aggregation behind `minion --effectiveness`.

The point of these numbers is to decide whether a cheaper model earns its keep,
so the tests focus on the ways that decision could be corrupted: unmetered rows
dragging averages down, an average-of-averages, a silent turn-ceiling failure
counted as a success, and cost-per-success on a window where nothing succeeded.
"""

import pytest

from minions.core.models import Agent, AgentRole, JobStatus
from minions.db.postgres import _derive, _roll_up


async def _agent(db, job_id, *, model, role=AgentRole.BACKEND_ENGINEER, cost=1.0, turns=10, status="done"):
    return await db.create_agent(
        Agent(
            job_id=job_id,
            role=role,
            model=model,
            status=status,
            cost_usd=cost,
            num_turns=turns,
            input_tokens=100,
            output_tokens=10,
        )
    )


class TestRollUp:
    """Pure aggregation. No database — this is where the arithmetic can be wrong."""

    def _row(self, **kw):
        base = {
            "model": "m",
            "role": "r",
            "difficulty": "easy",
            "runs": 1,
            "spend_usd": 1.0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "turns_total": 10,
            "max_turns": 10,
            "failed": 0,
            "ceiling_hits": 0,
        }
        base.update(kw)
        return _derive(base)

    def test_sums_counts_across_groups(self):
        rows = [self._row(role="a", runs=2, spend_usd=2.0), self._row(role="b", runs=3, spend_usd=4.0)]
        [merged] = _roll_up(rows, ("model",))
        assert merged["runs"] == 5
        assert merged["spend_usd"] == 6.0

    def test_avg_turns_is_weighted_not_an_average_of_averages(self):
        """The trap: 1 run at 100 turns and 99 at 1 turn is not 50.5 turns.

        Averaging the per-group averages would say ~50; the honest figure is
        ~2. Getting this wrong would make a model that occasionally runs away
        look uniformly slow, or hide it entirely, depending on group sizes.
        """
        rows = [
            self._row(role="a", runs=1, turns_total=100),
            self._row(role="b", runs=99, turns_total=99),
        ]
        [merged] = _roll_up(rows, ("model",))
        assert merged["runs"] == 100
        # 199 turns over 100 runs. The average of the two group averages would
        # be (100 + 1) / 2 = 50.5, which is the bug this guards against.
        assert merged["avg_turns"] == pytest.approx(2.0)

    def test_max_turns_takes_the_max_not_the_sum(self):
        rows = [self._row(role="a", max_turns=12), self._row(role="b", max_turns=97)]
        [merged] = _roll_up(rows, ("model",))
        assert merged["max_turns"] == 97

    def test_rates_are_recomputed_after_merge(self):
        rows = [
            self._row(role="a", runs=10, failed=0, ceiling_hits=0),
            self._row(role="b", runs=10, failed=5, ceiling_hits=2),
        ]
        [merged] = _roll_up(rows, ("model",))
        assert merged["failure_rate"] == pytest.approx(0.25)
        assert merged["ceiling_rate"] == pytest.approx(0.10)

    def test_zero_runs_does_not_divide_by_zero(self):
        assert _derive({"runs": 0, "spend_usd": 0.0, "turns_total": 0, "failed": 0, "ceiling_hits": 0})["avg_turns"] == 0.0

    def test_groups_stay_separate_on_a_composite_key(self):
        rows = [self._row(model="a", difficulty="easy"), self._row(model="a", difficulty="hard")]
        assert len(_roll_up(rows, ("model", "difficulty"))) == 2
        assert len(_roll_up(rows, ("model",))) == 1


@pytest.mark.asyncio
class TestModelEffectiveness:
    async def test_groups_by_model_with_cost_and_failures(self, db, sample_job):
        await _agent(db, sample_job.id, model="cheap", cost=1.0, turns=20, status="failed")
        await _agent(db, sample_job.id, model="cheap", cost=1.0, turns=20)
        await _agent(db, sample_job.id, model="dear", cost=10.0, turns=5)

        result = await db.get_model_effectiveness(days=30)
        by_model = {r["model"]: r for r in result["by_model"]}

        assert by_model["cheap"]["runs"] == 2
        assert by_model["cheap"]["spend_usd"] == pytest.approx(2.0)
        assert by_model["cheap"]["failure_rate"] == pytest.approx(0.5)
        assert by_model["dear"]["failure_rate"] == pytest.approx(0.0)
        # The whole point: cheap is cheaper per run and worse per run.
        assert by_model["cheap"]["cost_per_run_usd"] < by_model["dear"]["cost_per_run_usd"]
        assert by_model["cheap"]["avg_turns"] > by_model["dear"]["avg_turns"]

    async def test_herder_rows_are_excluded_and_counted_separately(self, db, sample_job):
        """External workers record $0.00 by design; averaged in they hide real cost.

        This is not hypothetical — 34 of the rows in the live database are
        `herder:<worker>` at zero dollars. Counting them as a model would halve
        every per-run figure and invent a flawless free model.
        """
        await _agent(db, sample_job.id, model="claude-sonnet-5", cost=6.0)
        await _agent(db, sample_job.id, model="herder:alex-nexus", cost=0.0)
        await _agent(db, sample_job.id, model="herder:w19p3", cost=0.0, status="failed")

        result = await db.get_model_effectiveness(days=30)

        assert [r["model"] for r in result["by_model"]] == ["claude-sonnet-5"]
        assert result["by_model"][0]["cost_per_run_usd"] == pytest.approx(6.0)
        assert result["external_runs"] == 2
        assert result["external_failed"] == 1

    async def test_turn_ceiling_hits_are_counted_though_status_is_done(self, db, sample_job):
        """Exhausting the turn budget leaves status=done, error=NULL.

        That is indistinguishable from a clean finish anywhere else in the
        system, so a model that quietly runs out of turns would score perfectly.
        """
        await _agent(db, sample_job.id, model="m", turns=100, status="done")
        # 50 is deliberately ABOVE `days` and below the ceiling. If the two
        # parameters are ever bound in the wrong order again, the filter becomes
        # `num_turns >= 30` and this row counts too, so the swap shows up as 2.
        await _agent(db, sample_job.id, model="m", turns=50, status="done")

        result = await db.get_model_effectiveness(days=30, turn_ceiling=100)
        row = result["by_model"][0]

        assert row["runs"] == 2
        assert row["failed"] == 0, "the run looks successful, which is exactly the problem"
        assert row["ceiling_hits"] == 1
        assert result["turn_ceiling"] == 100

    async def test_difficulty_stratification_separates_ticket_mix(self, db):
        """Comparing models across difficulties measures the classifier, not the model."""
        easy = await db.create_job("easy one")
        hard = await db.create_job("hard one")
        await db.update_job_difficulty(easy.id, "easy")
        await db.update_job_difficulty(hard.id, "hard")
        await _agent(db, easy.id, model="m", cost=1.0)
        await _agent(db, hard.id, model="m", cost=9.0)

        result = await db.get_model_effectiveness(days=30)
        strata = {r["difficulty"]: r for r in result["by_model_difficulty"]}

        assert strata["easy"]["spend_usd"] == pytest.approx(1.0)
        assert strata["hard"]["spend_usd"] == pytest.approx(9.0)

    async def test_unclassified_jobs_are_labelled_not_dropped(self, db, sample_job):
        await _agent(db, sample_job.id, model="m")
        result = await db.get_model_effectiveness(days=30)
        assert result["by_model_difficulty"][0]["difficulty"] == "unclassified"

    async def test_window_excludes_runs_outside_it(self, db, sample_job):
        """A zero-day window starts at NOW(), so a row written a moment ago is outside it."""
        await _agent(db, sample_job.id, model="m", cost=5.0)

        assert await db.get_model_effectiveness(days=30) != []
        narrow = await db.get_model_effectiveness(days=0)
        assert narrow["rows"] == []
        assert narrow["by_model"] == []
        assert narrow["external_runs"] == 0


@pytest.mark.asyncio
class TestOutcomeBreakdown:
    async def test_cost_per_success_amortises_failed_spend(self, db):
        """The headline number, and the one that makes a flaky model look expensive.

        Two jobs, one finished: $4 of work bought one success, so the cost of a
        success is $4 — not the $1 that looking only at the winner would show.
        """
        won = await db.create_job("won")
        lost = await db.create_job("lost")
        await _agent(db, won.id, model="m", cost=1.0)
        await _agent(db, lost.id, model="m", cost=3.0)
        await db.update_job_status(won.id, JobStatus.DONE)
        await db.update_job_status(lost.id, JobStatus.FAILED)

        out = await db.get_outcome_breakdown(days=30)

        assert out["total_spend_usd"] == pytest.approx(4.0)
        assert out["successful_jobs"] == 1
        assert out["cost_per_success_usd"] == pytest.approx(4.0)

    async def test_cost_per_success_is_none_when_nothing_finished(self, db, sample_job):
        """None, not 0.0 — a zero would render on a graph as 'free'."""
        await _agent(db, sample_job.id, model="m", cost=5.0)
        out = await db.get_outcome_breakdown(days=30)
        assert out["successful_jobs"] == 0
        assert out["cost_per_success_usd"] is None

    async def test_spend_is_attributed_per_status(self, db):
        won = await db.create_job("won")
        lost = await db.create_job("lost")
        await _agent(db, won.id, model="m", cost=2.0)
        await _agent(db, lost.id, model="m", cost=7.0)
        await db.update_job_status(won.id, JobStatus.DONE)
        await db.update_job_status(lost.id, JobStatus.FAILED)

        by_status = {r["status"]: r for r in (await db.get_outcome_breakdown(days=30))["by_status"]}

        assert by_status["failed"]["spend_usd"] == pytest.approx(7.0)
        assert by_status["done"]["spend_usd"] == pytest.approx(2.0)

    async def test_counts_rework_and_verdicts(self, db, sample_job, make_task):
        revised = make_task(sample_job.id, title="revised")
        revised.revision_count = 2
        revised.verdict = "request_changes"
        await db.create_task(revised)
        clean = make_task(sample_job.id, title="clean")
        clean.verdict = "approve"
        await db.create_task(clean)

        q = (await db.get_outcome_breakdown(days=30))["quality"]

        assert q["tasks_revised"] == 1
        assert q["revisions_total"] == 2
        assert q["revisions_max"] == 2
        assert q["verdict_approve"] == 1
        assert q["verdict_request_changes"] == 1

    async def test_empty_window_is_safe(self, db):
        out = await db.get_outcome_breakdown(days=30)
        assert out["total_spend_usd"] == 0
        assert out["cost_per_success_usd"] is None
        assert out["by_status"] == []


class TestMetricLines:
    """Exposition formatting. Prometheus rejects a malformed line silently."""

    def test_renders_labels_and_value(self):
        from minions.dashboard import _metric_lines

        lines = _metric_lines("m_spend", "help text", [({"model": "a", "role": "b"}, 1.5)])
        assert lines[0] == "# HELP m_spend help text"
        assert lines[1] == "# TYPE m_spend gauge"
        assert lines[2] == 'm_spend{model="a",role="b"} 1.5'

    def test_unlabelled_sample_has_no_braces(self):
        from minions.dashboard import _metric_lines

        assert _metric_lines("m", "h", [({}, 3)])[2] == "m 3"

    def test_escapes_quotes_and_newlines_in_labels(self):
        """A model string with a quote would otherwise produce an unparseable line."""
        from minions.dashboard import _metric_lines

        rendered = _metric_lines("m", "h", [({"model": 'we"ird\nname'}, 1)])[2]
        assert rendered == 'm{model="we\\"ird name"} 1'


@pytest.mark.asyncio
class TestRenderMetrics:
    async def test_emits_families_from_real_rows(self, db, sample_job, monkeypatch):
        import minions.dashboard as dash
        from tests.conftest import TEST_PG_URL

        monkeypatch.setattr(dash, "_postgres_url", TEST_PG_URL)
        await _agent(db, sample_job.id, model="claude-haiku-4-5", cost=2.5, turns=10)
        await db.update_job_status(sample_job.id, JobStatus.DONE)

        payload = await dash._render_metrics()

        assert 'minion_spend_usd{model="claude-haiku-4-5"' in payload
        assert "minion_cost_per_success_usd 2.5" in payload
        assert "# TYPE minion_agent_runs gauge" in payload
        # Every family must carry HELP/TYPE or Prometheus drops the metadata.
        assert payload.count("# HELP") == payload.count("# TYPE")

    async def test_omits_cost_per_success_when_nothing_finished(self, db, sample_job, monkeypatch):
        """Absent, not zero — a zero would render on a graph as 'successes are free'."""
        import minions.dashboard as dash
        from tests.conftest import TEST_PG_URL

        monkeypatch.setattr(dash, "_postgres_url", TEST_PG_URL)
        await _agent(db, sample_job.id, model="m", cost=5.0)

        assert "minion_cost_per_success_usd" not in await dash._render_metrics()
