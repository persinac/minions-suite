"""Stations: agents that work AROUND the line rather than on it.

The line is intake -> analyst -> arbiter -> engineer -> review -> merge, and it
is fed by `development` jobs. A station is something else -- scout, weight, the
deploy watcher, the verifier (openspec/changes/factory-stations/design.md). It
runs on its own trigger, with its own pinned model and its own daily budget,
and it must never borrow from the line:

- A station job does not occupy a line slot. Intake counts `max_concurrent_jobs`
  against `get_active_jobs()`, which returns every non-terminal job; with
  max_concurrent_jobs=1, one scout run would otherwise stop all card intake for
  as long as it ran. `line_jobs()` is the filter every capacity check uses.
- A station job does not count as a line success. `cost_per_success_usd` is the
  fully-loaded cost of one finished line job; folding scout runs into its
  denominator would make the line look cheaper the more the scouts ran.
- A station's spend is capped before it launches, per station and in total,
  by `check_station_budget()`. Station cost does not scale with any ticket, so
  the cap is flat dollars and runs per day, not a per-job ceiling.

The station name is also its job_type and its agent role, on purpose: one
string joins the jobs table, the agents table and the config.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

logger = logging.getLogger(__name__)

SCOUT = "scout"

# Every station that exists. A station not listed here is invisible to the
# budget, the metrics and the capacity filter -- add it here first.
STATIONS: frozenset[str] = frozenset({SCOUT})

# How far back "per day" looks. Rolling rather than a calendar day, so there is
# no midnight at which a station's whole allowance becomes available at once.
BUDGET_WINDOW = timedelta(hours=24)

# One budget-exhausted event per station per this long. The scheduler asks on
# every tick, and an event per tick would bury the events table in repeats of a
# fact that has not changed.
_EXHAUSTED_EVENT_INTERVAL = timedelta(hours=1)


def is_station_job(job) -> bool:
    """Whether a job belongs to a station rather than to the line."""
    return (getattr(job, "job_type", "") or "") in STATIONS


def line_jobs(jobs: list) -> list:
    """The jobs that occupy line capacity -- every job that is not a station's."""
    return [j for j in jobs if not is_station_job(j)]


def station_model(config, station: str) -> str:
    """The model a station runs on: its `[engine.role_models]` pin.

    Deliberately NOT resolve_model(). Difficulty routing exists because engineer
    cost scales with how hard the ticket is; a station has no ticket, so routing
    it by difficulty would only add variance. An unpinned station falls to the
    medium tier rather than to `config.model`, which is Opus.
    """
    pinned = (getattr(config, "role_models", None) or {}).get(station, "")
    if pinned:
        return pinned
    return config.model_medium or config.model


@dataclass(frozen=True)
class StationBudget:
    daily_usd: float
    max_runs_per_day: int


def station_budget(config, station: str) -> StationBudget:
    """The configured budget for one station. A station with none gets zero.

    Zero, not unlimited: a station someone forgot to budget should be unable to
    spend, not free to. That is the opposite of the line's limits, where 0 means
    "off", and it is deliberate -- the line's limits guard against a runaway
    job; a station's guard against a station nobody decided to pay for.
    """
    raw = (getattr(config, "station_budgets", None) or {}).get(station) or {}
    return StationBudget(
        daily_usd=float(raw.get("daily_usd", 0.0) or 0.0),
        max_runs_per_day=int(raw.get("max_runs_per_day", 0) or 0),
    )


@dataclass(frozen=True)
class BudgetDecision:
    allowed: bool
    reason: str
    runs: int
    spend_usd: float
    total_spend_usd: float


class StationBudgetGuard:
    """Decides whether a station may launch, and records when it may not.

    Owned by the engine. The only state it holds is when it last recorded an
    exhausted event, which is a de-duplication convenience: losing it on restart
    costs one repeated event, never an extra launch -- the decision itself is
    read from the database every time.
    """

    def __init__(self) -> None:
        self._last_exhausted_event: dict[str, datetime] = {}

    async def check(self, db, config, station: str, now: datetime | None = None, include_runs: bool = True) -> BudgetDecision:
        """Whether `station` may launch one more run right now.

        Read BEFORE launch: once the model is called the money is spent, so a
        check after the fact only reports an overrun. Three caps, any one of
        which refuses: the station's runs today, the station's spend today, and
        the spend of every station together.

        `include_runs=False` is for the second check, made just before the model
        call of a run that was already admitted. The run itself now exists and
        counts toward the run cap, so applying that cap again would refuse the
        run it just admitted; the spend caps still apply.
        """
        if now is None:
            now = datetime.now(UTC)
        since = (now - BUDGET_WINDOW).isoformat()
        usage = await db.get_station_usage(sorted(STATIONS), since)
        mine = usage.get(station, {"runs": 0, "spend_usd": 0.0})
        runs = int(mine["runs"])
        spend = float(mine["spend_usd"])
        total = sum(float(u["spend_usd"]) for u in usage.values())

        budget = station_budget(config, station)
        total_cap = float(getattr(config, "station_total_daily_usd", 0.0) or 0.0)

        reason = ""
        if include_runs and runs >= budget.max_runs_per_day:
            reason = f"{station} has run {runs} time(s) in 24h, cap {budget.max_runs_per_day}"
        elif spend >= budget.daily_usd:
            reason = f"{station} has spent ${spend:.2f} in 24h, cap ${budget.daily_usd:.2f}"
        elif total >= total_cap:
            reason = f"all stations have spent ${total:.2f} in 24h, cap ${total_cap:.2f}"

        decision = BudgetDecision(allowed=not reason, reason=reason, runs=runs, spend_usd=spend, total_spend_usd=total)
        if not decision.allowed:
            await self._record_exhausted(db, station, reason, now)
        return decision

    async def _record_exhausted(self, db, station: str, reason: str, now: datetime) -> None:
        last = self._last_exhausted_event.get(station)
        if last is not None and now - last < _EXHAUSTED_EVENT_INTERVAL:
            return
        self._last_exhausted_event[station] = now
        logger.info("Station %s not launched: %s", station, reason)
        await db.record_event(None, "station_budget_exhausted", station, reason)
