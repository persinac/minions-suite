"""Station rails: the budget a station must clear before it launches, and the
rule that a station never borrows from the line.

openspec/changes/factory-stations/design.md, "The station contract". The guards
here are the whole defence against a station spending money nobody approved, so
each refusal is tested on both sides of its threshold.
"""

from datetime import UTC, datetime, timedelta

import pytest

from minions.config import Config
from minions.core.models import Agent, AgentRole, Job, JobStatus
from minions.core.stations import (
    SCOUT,
    STATIONS,
    StationBudgetGuard,
    is_station_job,
    line_jobs,
    station_budget,
    station_model,
)


def _config(**overrides) -> Config:
    config = Config.from_env()
    config.role_models = {SCOUT: "claude-sonnet-5"}
    config.station_budgets = {SCOUT: {"daily_usd": 2.0, "max_runs_per_day": 3}}
    config.station_total_daily_usd = 3.0
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


async def _station_job(db, station: str = SCOUT, created_at: str | None = None) -> Job:
    """A job of a station's type. create_job only makes development jobs."""
    job = await db.create_job(f"{station} run")
    import minions.db.postgres as pg

    async with db._pool.connection() as conn:
        if created_at:
            await conn.execute(f"UPDATE {pg.JOB_SCHEMA}.jobs SET job_type = %s, created_at = %s WHERE id = %s", (station, created_at, job.id))
        else:
            await conn.execute(f"UPDATE {pg.JOB_SCHEMA}.jobs SET job_type = %s WHERE id = %s", (station, job.id))
    return await db.get_job(job.id)


async def _spend(db, job_id: str, cost: float) -> None:
    await db.create_agent(Agent(job_id=job_id, role=AgentRole.SPEC_ANALYST, model="m", status="done", cost_usd=cost, num_turns=3))


class TestClassification:
    def test_a_station_job_is_not_a_line_job(self):
        line = Job(spec="card", job_type="development")
        review = Job(spec="mr", job_type="review")
        scout = Job(spec="scout", job_type=SCOUT)

        assert is_station_job(scout)
        assert not is_station_job(line)
        assert line_jobs([line, review, scout]) == [line, review]

    def test_every_station_is_known_by_one_name(self):
        """Station name == job_type == agent role, so one string joins all three."""
        assert SCOUT in STATIONS


class TestModelPin:
    def test_a_pinned_station_uses_its_pin(self):
        assert station_model(_config(), SCOUT) == "claude-sonnet-5"

    def test_difficulty_never_moves_a_station(self):
        """A station has no ticket; the tier models must not leak in."""
        config = _config(model_easy="tier-easy", model_hard="tier-hard")
        assert station_model(config, SCOUT) == "claude-sonnet-5"

    def test_an_unpinned_station_falls_to_medium_not_opus(self):
        config = _config(role_models={}, model_medium="tier-medium", model="claude-opus-5")
        assert station_model(config, SCOUT) == "tier-medium"


class TestBudgetConfig:
    def test_an_unbudgeted_station_gets_zero_not_unlimited(self):
        budget = station_budget(_config(station_budgets={}), SCOUT)
        assert budget.daily_usd == 0.0
        assert budget.max_runs_per_day == 0


@pytest.mark.asyncio
class TestBudgetGuard:
    async def test_a_fresh_station_may_launch(self, db):
        decision = await StationBudgetGuard().check(db, _config(), SCOUT)
        assert decision.allowed, decision.reason

    async def test_the_run_cap_refuses_at_the_limit(self, db):
        config = _config()
        for _ in range(2):
            await _station_job(db)
        assert (await StationBudgetGuard().check(db, config, SCOUT)).allowed

        await _station_job(db)
        decision = await StationBudgetGuard().check(db, config, SCOUT)

        assert not decision.allowed
        assert decision.runs == 3
        assert "cap 3" in decision.reason

    async def test_the_station_spend_cap_refuses_at_the_limit(self, db):
        config = _config(station_budgets={SCOUT: {"daily_usd": 2.0, "max_runs_per_day": 99}})
        job = await _station_job(db)
        await _spend(db, job.id, 1.99)
        assert (await StationBudgetGuard().check(db, config, SCOUT)).allowed

        await _spend(db, job.id, 0.01)
        decision = await StationBudgetGuard().check(db, config, SCOUT)

        assert not decision.allowed
        assert decision.spend_usd == pytest.approx(2.0)

    async def test_the_all_stations_cap_refuses_even_under_the_station_cap(self, db):
        config = _config(station_budgets={SCOUT: {"daily_usd": 50.0, "max_runs_per_day": 99}}, station_total_daily_usd=3.0)
        job = await _station_job(db)
        await _spend(db, job.id, 3.0)

        decision = await StationBudgetGuard().check(db, config, SCOUT)

        assert not decision.allowed
        assert "all stations" in decision.reason

    async def test_line_spend_never_counts_against_a_station(self, db):
        """The two budgets are separate: an expensive engineer day must not
        starve the scout, and the scout must not be charged for it."""
        line = await db.create_job("a card")
        await _spend(db, line.id, 500.0)

        assert (await StationBudgetGuard().check(db, _config(), SCOUT)).allowed

    async def test_runs_outside_the_window_do_not_count(self, db):
        old = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
        for _ in range(5):
            await _station_job(db, created_at=old)

        assert (await StationBudgetGuard().check(db, _config(), SCOUT)).allowed

    async def test_exhaustion_is_recorded_once_per_hour_not_once_per_tick(self, db):
        config = _config(station_budgets={SCOUT: {"daily_usd": 2.0, "max_runs_per_day": 0}})
        guard = StationBudgetGuard()
        now = datetime.now(UTC)

        await guard.check(db, config, SCOUT, now=now)
        await guard.check(db, config, SCOUT, now=now + timedelta(minutes=5))
        assert len(await _exhausted_events(db)) == 1

        await guard.check(db, config, SCOUT, now=now + timedelta(minutes=61))
        events = await _exhausted_events(db)
        assert len(events) == 2
        assert events[0]["source"] == SCOUT


async def _exhausted_events(db) -> list[dict]:
    import minions.db.postgres as pg

    async with db._pool.connection() as conn:
        cur = await conn.execute(f"SELECT * FROM {pg.JOB_SCHEMA}.events WHERE event_type = 'station_budget_exhausted' ORDER BY id")
        return [dict(r) for r in await cur.fetchall()]


@pytest.mark.asyncio
class TestStationsStayOffTheLineMetrics:
    async def test_a_done_station_run_is_not_a_line_success(self, db):
        """Otherwise cost per success falls every time a scout runs."""
        line = await db.create_job("a card")
        await _spend(db, line.id, 4.0)
        await db.update_job_status(line.id, JobStatus.DONE)
        scout = await _station_job(db)
        await _spend(db, scout.id, 1.0)
        await db.update_job_status(scout.id, JobStatus.DONE)

        out = await db.get_outcome_breakdown(days=30)

        assert out["successful_jobs"] == 1
        assert out["cost_per_success_usd"] == pytest.approx(4.0)

    async def test_station_outcomes_report_runs_and_spend(self, db):
        done = await _station_job(db)
        await _spend(db, done.id, 0.5)
        await db.update_job_status(done.id, JobStatus.DONE)
        await _station_job(db)

        rows = await db.get_station_outcomes(sorted(STATIONS), days=30)

        by_outcome = {r["outcome"]: r for r in rows}
        assert by_outcome["done"]["runs"] == 1
        assert by_outcome["done"]["spend_usd"] == pytest.approx(0.5)
        assert by_outcome["running"]["runs"] == 1

    async def test_metrics_carry_the_station_families(self, db, monkeypatch):
        import minions.dashboard as dash
        from tests.conftest import TEST_PG_URL

        monkeypatch.setattr(dash, "_postgres_url", TEST_PG_URL)
        scout = await _station_job(db)
        await _spend(db, scout.id, 0.25)
        await db.update_job_status(scout.id, JobStatus.DONE)

        payload = await dash._render_metrics()

        assert 'minion_station_runs_total{station="scout",outcome="done"} 1' in payload
        assert 'minion_station_spend_usd{station="scout"} 0.25' in payload
        assert payload.count("# HELP") == payload.count("# TYPE")

    async def test_an_idle_station_reports_zero_spend_not_nothing(self, db, monkeypatch):
        import minions.dashboard as dash
        from tests.conftest import TEST_PG_URL

        monkeypatch.setattr(dash, "_postgres_url", TEST_PG_URL)

        payload = await dash._render_metrics()

        assert 'minion_station_spend_usd{station="scout"} 0.0' in payload
