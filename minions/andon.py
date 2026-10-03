"""Andon — stop-the-line alarms that reach a human.

The line stopped on 2026-09-26 and stayed stopped for seven days. Job a9f2b36e
held the only job slot behind a herder claim whose pane was long gone, so the
Trello poller admitted nothing and the ten cards queued behind it sat still.
Nothing in the system was broken in a way that logged an error. The one thing
that noticed was the hourly groomer, which wrote "this needs a human" into
journald every hour for two days, where no human reads.

So this module does one job: when the line is stopped, say so to a person, in
a DM, with the id and the next thing to do. It never fixes anything itself —
the engine's own guards do that, and an alarm that also acted would hide the
guard that failed.

Three conditions, all derived from the database so they survive restarts and
agree across processes:

  stalled_job          (engine) a non-terminal job with no recorded activity
                       for andon_stall_seconds.
  stale_claim          (engine) a herder claim still "running" past
                       herder_work_timeout_seconds + andon_claim_grace_seconds.
                       The engine's guard should already have released it, so
                       this firing means that guard failed.
  line_idle_with_queue (Trello poller) minion cards wait in On-deck and no job
                       has been created for trello_min_job_interval +
                       andon_idle_seconds. Raised from the poller, which runs
                       in a different pod, so it still fires when the engine
                       process is the thing that died.

Dedupe is persisted as events (andon_raised / andon_cleared, detail starting
with "<condition>:<subject>"), so a restart neither re-sends nor forgets. A
raised condition is re-sent at most every andon_repeat_seconds, and one
"cleared" DM goes out when it resolves.

Deterministic on purpose: no model call. An alarm that can hallucinate, rate
limit or cost money is one more thing that can be quietly broken.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .config import Config
from .core.models import Agent, Job, Task
from .core.stations import line_jobs
from .db import AbstractDatabase
from .notify import notify

logger = logging.getLogger(__name__)

STALLED_JOB = "stalled_job"
STALE_CLAIM = "stale_claim"
LINE_IDLE = "line_idle_with_queue"
CONDITIONS = (STALLED_JOB, STALE_CLAIM, LINE_IDLE)

# The conditions each process owns. A process only CLEARS what it raises: the
# engine cannot see the queue, so if it cleared line_idle_with_queue because it
# did not find it, every engine check would undo every poller alarm.
ENGINE_CONDITIONS = (STALLED_JOB, STALE_CLAIM)
POLLER_CONDITIONS = (LINE_IDLE,)

EVENT_RAISED = "andon_raised"
EVENT_CLEARED = "andon_cleared"

# Events that are NOT evidence the job is moving. The andon's own events would
# reset the stall clock every time it fired. transition_rejected is written on
# every poll by a job stuck against the state machine, which is the opposite of
# progress.
_NOT_ACTIVITY = {EVENT_RAISED, EVENT_CLEARED, "transition_rejected"}


@dataclass(frozen=True)
class Alarm:
    condition: str
    subject: str
    job_id: str | None
    message: str

    @property
    def key(self) -> str:
        return f"{self.condition}:{self.subject}"


def parse_ts(value) -> datetime | None:
    """An ISO string or datetime as an aware UTC datetime, or None if unreadable."""
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def format_age(seconds: float) -> str:
    """'7d 2h', '3h 5m', '45m' — how long, in the words a person would say."""
    minutes = int(seconds // 60)
    days, minutes = divmod(minutes, 24 * 60)
    hours, minutes = divmod(minutes, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _title(job: Job) -> str:
    text = (job.spec or "").strip()
    if not text:
        return ""
    return text.splitlines()[0].lstrip("# ").strip()[:90]


def last_activity(job: Job, tasks: list[Task], agents: list[Agent], events: list[dict]) -> datetime | None:
    """The newest timestamp that shows this job doing anything.

    A running agent does NOT count as activity for as long as it runs — only
    its start and finish do. That is the whole point: the agent behind job
    a9f2b36e read "running" for seven days.
    """
    stamps = [parse_ts(job.created_at), parse_ts(job.updated_at)]
    for task in tasks:
        stamps.append(parse_ts(task.created_at))
        stamps.append(parse_ts(task.updated_at))
    for agent in agents:
        stamps.append(parse_ts(agent.started_at))
        stamps.append(parse_ts(agent.finished_at))
    for event in events:
        if event.get("event_type") in _NOT_ACTIVITY:
            continue
        stamps.append(parse_ts(event.get("created_at")))
    known = [s for s in stamps if s is not None]
    if not known:
        return None
    return max(known)


async def find_stale_claims(db: AbstractDatabase, config: Config, now: datetime) -> list[Alarm]:
    """Herder claims still 'running' well past the point the engine should have released them."""
    if config.herder_work_timeout_seconds <= 0:
        return []
    limit = config.herder_work_timeout_seconds + config.andon_claim_grace_seconds
    alarms = []
    for agent in await db.get_running_agents():
        if agent.status != "running":
            continue
        if not str(agent.model or "").startswith("herder:"):
            continue
        started = parse_ts(agent.started_at)
        if started is None:
            continue
        age = (now - started).total_seconds()
        if age < limit:
            continue
        message = "\n".join(
            [
                f":rotating_light: Line stopped: herder claim `{agent.id}` on job `{agent.job_id}` has been open {format_age(age)}.",
                f"• The engine should have released it after {format_age(config.herder_work_timeout_seconds)}. It did not, so that guard is broken.",
                f"• Next: release it with `release_engineer_work(agent_id={agent.id})`, then file the guard bug.",
            ]
        )
        alarms.append(Alarm(STALE_CLAIM, agent.id, agent.job_id, message))
    return alarms


async def find_stalled_jobs(db: AbstractDatabase, config: Config, now: datetime, skip_job_ids: set[str] | None = None) -> list[Alarm]:
    """Active jobs with no recorded activity for andon_stall_seconds.

    skip_job_ids folds a job into a stale_claim alarm already raised for it.
    The claim alarm comes first (its limit runs from the agent's start, the
    stall limit from the latest activity, which is never earlier) and names
    the exact fix, so a second DM about the same job would only be noise.
    """
    skip = skip_job_ids or set()
    alarms = []
    for job in await db.get_active_jobs():
        if job.id in skip:
            continue
        tasks = await db.get_tasks(job.id)
        agents = await db.get_agents_for_job(job.id)
        events = await db.get_events(job.id)
        latest = last_activity(job, tasks, agents, events)
        if latest is None:
            continue
        age = (now - latest).total_seconds()
        if age < config.andon_stall_seconds:
            continue
        message = "\n".join(
            [
                f":rotating_light: Line stopped: job `{job.id}` (*{job.status}*) has not moved in {format_age(age)}. {_title(job)}",
                "• It holds a job slot, so cards behind it wait too.",
                f"• Next: run `get_job_status {job.id}`. Release a stuck claim with `release_engineer_work`, or kill the job with `scripts/kill-job.sh {job.id}`.",
            ]
        )
        alarms.append(Alarm(STALLED_JOB, job.id, job.id, message))
    return alarms


async def find_idle_line(db: AbstractDatabase, config: Config, now: datetime, waiting_cards: int) -> list[Alarm]:
    """Cards are queued, yet no job has been created for well past the intake throttle.

    The intake throttle (trello_min_job_interval) lets one job start per
    window by design, so the clock starts after it: in production that is 4h
    of throttle plus andon_idle_seconds before this fires.
    """
    if waiting_cards <= 0:
        return []
    window = config.trello_min_job_interval + config.andon_idle_seconds
    since = (now - timedelta(seconds=window)).isoformat()
    if await db.count_jobs_since(since) > 0:
        return []
    # Line jobs only: a station run (core/stations.py) never holds an intake
    # slot, so naming one here would send the reader to the wrong job.
    active = line_jobs(await db.get_active_jobs())
    if active:
        holders = ", ".join(f"`{j.id}` (*{j.status}*)" for j in active[:3])
        reason = f"• Active job(s) holding the slot: {holders}."
        action = "• Next: check that job with `get_job_status`. If it is stuck, release or kill it."
    else:
        reason = "• No job is active, so intake itself is not launching."
        action = "• Next: check the input-sources pod logs, then the engine pod."
    message = "\n".join(
        [
            f":rotating_light: Line stopped: {waiting_cards} card(s) wait in On-deck and no job has started in over {format_age(window)}.",
            reason,
            action,
        ]
    )
    return [Alarm(LINE_IDLE, "intake", None, message)]


def raised_keys(events: list[dict]) -> dict[str, datetime]:
    """Alarm keys currently raised, mapped to when each was last raised.

    A key is raised when its newest andon_raised is newer than its newest
    andon_cleared. Keys are the first token of the event detail.
    """
    raised: dict[str, datetime] = {}
    cleared: dict[str, datetime] = {}
    for event in events:
        detail = str(event.get("detail") or "")
        key = detail.split(" ", 1)[0]
        stamp = parse_ts(event.get("created_at"))
        if not key or stamp is None:
            continue
        if event.get("event_type") == EVENT_RAISED:
            book = raised
        elif event.get("event_type") == EVENT_CLEARED:
            book = cleared
        else:
            continue
        if key not in book or stamp > book[key]:
            book[key] = stamp
    return {key: stamp for key, stamp in raised.items() if key not in cleared or stamp > cleared[key]}


def _lookback_seconds(config: Config) -> int:
    # A condition that is still active is re-raised every andon_repeat_seconds,
    # so its newest raised event is never older than that plus one check. Twice
    # the repeat window is ample, and bounds the query.
    return 2 * config.andon_repeat_seconds + config.andon_check_interval_seconds


async def load_raised(db: AbstractDatabase, config: Config, now: datetime) -> dict[str, datetime]:
    since = (now - timedelta(seconds=_lookback_seconds(config))).isoformat()
    events = await db.get_events_by_type([EVENT_RAISED, EVENT_CLEARED], since)
    return raised_keys(events)


async def _send(config: Config, message: str) -> bool:
    if not config.slack_enabled:
        return False
    return await notify(config.slack_webhook_url, message, bot_token=config.slack_bot_token, target=config.slack_dm_target)


def _record_it(sent: bool, config: Config) -> bool:
    # With Slack configured, record only what was delivered, so a Slack outage
    # is retried on the next check rather than swallowed for a whole repeat
    # window. With Slack off, the log line is the only channel: record it so
    # the warning repeats on the andon's cadence, not on every check.
    if sent:
        return True
    return not config.slack_enabled


async def publish(db: AbstractDatabase, config: Config, alarms: list[Alarm], conditions: tuple[str, ...], source: str, now: datetime) -> dict:
    """Send what is newly raised or due a repeat, and clear what resolved. Returns what it did."""
    raised = await load_raised(db, config, now)
    active_keys = {alarm.key for alarm in alarms}
    sent_keys = []
    cleared_keys = []

    for alarm in alarms:
        last = raised.get(alarm.key)
        if last is not None and (now - last).total_seconds() < config.andon_repeat_seconds:
            continue
        logger.warning("ANDON %s: %s", alarm.key, alarm.message.splitlines()[0])
        sent = await _send(config, alarm.message)
        if _record_it(sent, config):
            channel = "dm" if sent else "log-only"
            await db.record_event(alarm.job_id, EVENT_RAISED, source, f"{alarm.key} {channel}")
        sent_keys.append(alarm.key)

    for key in raised:
        condition = key.split(":", 1)[0]
        if condition not in conditions or key in active_keys:
            continue
        subject = key.split(":", 1)[1] if ":" in key else key
        message = f":white_check_mark: Line moving again: `{subject}` ({condition}) cleared."
        logger.info("ANDON cleared %s", key)
        sent = await _send(config, message)
        if _record_it(sent, config):
            job_id = None
            if condition == STALLED_JOB:
                job_id = subject
            await db.record_event(job_id, EVENT_CLEARED, source, f"{key} {'dm' if sent else 'log-only'}")
        cleared_keys.append(key)

    return {"raised": sent_keys, "cleared": cleared_keys}


async def check_engine(db: AbstractDatabase, config: Config, now: datetime | None = None) -> dict:
    """One engine-side pass: stale claims, then stalled jobs not already covered by one."""
    if now is None:
        now = datetime.now(UTC)
    claims = await find_stale_claims(db, config, now)
    covered = {alarm.job_id for alarm in claims if alarm.job_id}
    stalled = await find_stalled_jobs(db, config, now, skip_job_ids=covered)
    return await publish(db, config, claims + stalled, ENGINE_CONDITIONS, "andon", now)


async def check_intake(db: AbstractDatabase, config: Config, waiting_cards: int, now: datetime | None = None) -> dict:
    """One poller-side pass: is the queue full while nothing starts?"""
    if now is None:
        now = datetime.now(UTC)
    alarms = await find_idle_line(db, config, now, waiting_cards)
    return await publish(db, config, alarms, POLLER_CONDITIONS, "andon-intake", now)
