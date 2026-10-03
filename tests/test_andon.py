"""The andon: when the line stops, a human hears about it.

The line stopped on 2026-09-26 and stayed stopped for seven days. Job a9f2b36e
held the only slot behind a herder claim whose pane was gone; ten queued cards
sat behind it. Nothing logged an error. The only thing that noticed was the
hourly groomer, writing "needs a human" into journald.

These tests use an injected `now` instead of backdating rows: jobs.updated_at
and tasks.updated_at are forced to NOW() by a BEFORE UPDATE trigger, so the
honest way to make something old is to look at it from the future.

Each condition is tested both ways — it fires on the stuck state AND stays
silent on a healthy one — because an alarm that pages on a working line gets
muted, and a muted alarm is the silence this replaced.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

from minions import andon
from minions.config import Config
from minions.core.models import Agent, AgentRole, JobStatus


def _config(**overrides) -> Config:
    """Defaults, Slack off. Slack off still records events (log-only), which is what dedupe reads."""
    config = Config()
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


def _slack_config(**overrides) -> Config:
    return _config(slack_bot_token="xoxb-not-a-real-token", slack_dm_target="U000TEST", **overrides)


def _at(stamp: str, seconds: float) -> datetime:
    return andon.parse_ts(stamp) + timedelta(seconds=seconds)


async def _raised_events(db, condition: str | None = None) -> list[dict]:
    since = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    events = await db.get_events_by_type([andon.EVENT_RAISED], since)
    if condition is None:
        return events
    return [e for e in events if str(e["detail"]).startswith(condition + ":")]


async def _herder(db, job_id: str, task_id: str | None = None, model: str = "herder:herder-w4D-p1", status: str = "running") -> Agent:
    agent = await db.create_agent(Agent(model=model, job_id=job_id, task_id=task_id, role=AgentRole.FRONTEND_ENGINEER))
    await db.update_agent(agent.id, status=status)
    return await db.get_agent(agent.id)


# --------------------------------------------------------------------------
# stalled_job
# --------------------------------------------------------------------------


class TestStalledJob:
    async def test_a_job_with_no_activity_past_the_limit_fires(self, db, sample_job):
        config = _config()
        now = _at(sample_job.updated_at, config.andon_stall_seconds + 60)

        alarms = await andon.find_stalled_jobs(db, config, now)

        assert [a.key for a in alarms] == [f"stalled_job:{sample_job.id}"]
        assert sample_job.id in alarms[0].message
        assert "kill-job.sh" in alarms[0].message, "the DM must carry the next action, not just the fact"

    async def test_a_job_that_moved_recently_is_silent(self, db, sample_job):
        config = _config()
        now = _at(sample_job.updated_at, 10 * 60)

        assert await andon.find_stalled_jobs(db, config, now) == []

    async def test_a_running_agent_is_not_activity(self, db, sample_job):
        """The agent behind a9f2b36e read "running" for seven days. Only its start counts."""
        config = _config()
        agent = await _herder(db, sample_job.id, model="claude-sonnet-5")
        now = _at(agent.started_at, config.andon_stall_seconds + 60)

        alarms = await andon.find_stalled_jobs(db, config, now)

        assert [a.subject for a in alarms] == [sample_job.id]

    async def test_a_finished_agent_restarts_the_clock(self, db, sample_job):
        config = _config()
        agent = await _herder(db, sample_job.id, model="claude-sonnet-5")
        finished = _at(agent.started_at, config.andon_stall_seconds)
        await db.update_agent(agent.id, status="done", finished_at=finished.isoformat())

        # Old relative to the job's own rows, recent relative to the agent's finish.
        now = finished + timedelta(minutes=10)
        assert await andon.find_stalled_jobs(db, config, now) == []

    async def test_its_own_events_do_not_reset_the_clock(self, db, sample_job):
        """Raising writes an event on the job. If that counted, the alarm would silence itself."""
        config = _config()
        await db.record_event(sample_job.id, andon.EVENT_RAISED, "andon", f"stalled_job:{sample_job.id} dm")
        await db.record_event(sample_job.id, "transition_rejected", "engine", "stuck against the state machine")
        now = _at(sample_job.updated_at, config.andon_stall_seconds + 60)

        assert [a.subject for a in await andon.find_stalled_jobs(db, config, now)] == [sample_job.id]

    async def test_a_terminal_job_never_fires(self, db, sample_job):
        config = _config()
        await db.update_job_status(sample_job.id, JobStatus.FAILED, error="killed")
        now = datetime.now(UTC) + timedelta(days=30)

        assert await andon.find_stalled_jobs(db, config, now) == []


# --------------------------------------------------------------------------
# stale_claim
# --------------------------------------------------------------------------


class TestStaleClaim:
    async def test_a_herder_claim_past_timeout_plus_grace_fires(self, db, sample_job):
        config = _config()
        agent = await _herder(db, sample_job.id)
        now = _at(agent.started_at, config.herder_work_timeout_seconds + config.andon_claim_grace_seconds + 60)

        alarms = await andon.find_stale_claims(db, config, now)

        assert [a.key for a in alarms] == [f"stale_claim:{agent.id}"]
        assert alarms[0].job_id == sample_job.id
        assert f"release_engineer_work(agent_id={agent.id})" in alarms[0].message
        assert "guard is broken" in alarms[0].message, "this firing means the engine's guard failed; say so"

    async def test_a_working_herder_is_silent(self, db, sample_job):
        """Real herder runs take 5-45 minutes."""
        config = _config()
        agent = await _herder(db, sample_job.id)
        for minutes in (5, 20, 44, 59):
            now = _at(agent.started_at, minutes * 60)
            assert await andon.find_stale_claims(db, config, now) == [], f"paged on a herder at {minutes} min"

    async def test_an_in_process_agent_is_not_a_claim(self, db, sample_job):
        config = _config()
        agent = await _herder(db, sample_job.id, model="claude-sonnet-5")
        now = _at(agent.started_at, 10 * config.herder_work_timeout_seconds)

        assert await andon.find_stale_claims(db, config, now) == []

    async def test_a_closed_claim_is_silent(self, db, sample_job):
        config = _config()
        agent = await _herder(db, sample_job.id, status="failed")
        now = _at(agent.started_at, 10 * config.herder_work_timeout_seconds)

        assert await andon.find_stale_claims(db, config, now) == []

    async def test_one_incident_is_one_dm(self, db, sample_job):
        """A stale claim also stalls its job. The claim DM names the fix, so the job DM is folded in."""
        config = _config()
        agent = await _herder(db, sample_job.id)
        now = _at(agent.started_at, config.andon_stall_seconds + config.herder_work_timeout_seconds)

        result = await andon.check_engine(db, config, now)

        assert result["raised"] == [f"stale_claim:{agent.id}"]


# --------------------------------------------------------------------------
# line_idle_with_queue
# --------------------------------------------------------------------------


class TestIdleLine:
    async def test_cards_waiting_with_no_job_ever_fires(self, db):
        config = _config()

        alarms = await andon.find_idle_line(db, config, datetime.now(UTC), waiting_cards=3)

        assert [a.key for a in alarms] == ["line_idle_with_queue:intake"]
        assert "3 card(s)" in alarms[0].message
        assert "No job is active" in alarms[0].message

    async def test_an_empty_queue_is_silent(self, db):
        config = _config()
        assert await andon.find_idle_line(db, config, datetime.now(UTC) + timedelta(days=30), waiting_cards=0) == []

    async def test_a_recent_job_is_silent(self, db, sample_job):
        config = _config()
        assert await andon.find_idle_line(db, config, datetime.now(UTC), waiting_cards=3) == []

    async def test_the_throttle_is_not_an_outage(self, db, sample_job):
        """Prod admits one job per 4h on purpose. The idle clock starts after that."""
        config = _config(trello_min_job_interval=14400, andon_idle_seconds=7200)
        inside = _at(sample_job.created_at, 14400 + 3600)
        past = _at(sample_job.created_at, 14400 + 7200 + 60)

        assert await andon.find_idle_line(db, config, inside, waiting_cards=3) == []
        alarms = await andon.find_idle_line(db, config, past, waiting_cards=3)
        assert [a.subject for a in alarms] == ["intake"]
        assert sample_job.id in alarms[0].message, "name the job holding the slot"


# --------------------------------------------------------------------------
# Delivery and dedupe
# --------------------------------------------------------------------------


class TestDedupe:
    async def test_it_dms_once_then_holds_until_the_repeat_window(self, db, sample_job):
        config = _slack_config()
        sent = AsyncMock(return_value=True)
        start = _at(sample_job.updated_at, config.andon_stall_seconds + 60)

        with patch("minions.andon.notify", sent):
            first = await andon.check_engine(db, config, start)
            again = await andon.check_engine(db, config, start + timedelta(minutes=10))
            later = await andon.check_engine(db, config, datetime.now(UTC) + timedelta(seconds=config.andon_repeat_seconds + 60))

        assert first["raised"] == [f"stalled_job:{sample_job.id}"]
        assert again["raised"] == [], "re-sent inside the repeat window"
        assert later["raised"] == [f"stalled_job:{sample_job.id}"], "never re-sent a still-stuck job"
        assert sent.await_count == 2
        assert sent.await_args.kwargs["target"] == "U000TEST"

    async def test_dedupe_survives_a_restart(self, db, sample_job):
        """State lives in events, not in the process — a fresh check sees the old raise."""
        config = _config()
        start = _at(sample_job.updated_at, config.andon_stall_seconds + 60)
        await andon.check_engine(db, config, start)

        assert len(await _raised_events(db, "stalled_job")) == 1
        assert (await andon.check_engine(db, config, start + timedelta(hours=1)))["raised"] == []

    async def test_a_failed_dm_is_retried_not_swallowed(self, db, sample_job):
        config = _slack_config()
        start = _at(sample_job.updated_at, config.andon_stall_seconds + 60)

        with patch("minions.andon.notify", AsyncMock(return_value=False)):
            await andon.check_engine(db, config, start)
        assert await _raised_events(db) == [], "recorded a DM Slack refused, so it would never be retried"

        with patch("minions.andon.notify", AsyncMock(return_value=True)) as ok:
            result = await andon.check_engine(db, config, start + timedelta(minutes=5))
        assert result["raised"] == [f"stalled_job:{sample_job.id}"]
        assert ok.await_count == 1

    async def test_it_says_when_the_line_moves_again(self, db, sample_job):
        config = _slack_config()
        start = _at(sample_job.updated_at, config.andon_stall_seconds + 60)
        sent = AsyncMock(return_value=True)

        with patch("minions.andon.notify", sent):
            await andon.check_engine(db, config, start)
            await db.update_job_status(sample_job.id, JobStatus.FAILED, error="killed by hand")
            cleared = await andon.check_engine(db, config, start + timedelta(minutes=5))
            quiet = await andon.check_engine(db, config, start + timedelta(minutes=10))

        assert cleared["cleared"] == [f"stalled_job:{sample_job.id}"]
        assert "Line moving again" in sent.await_args_list[1].args[1]
        assert quiet == {"raised": [], "cleared": []}, "cleared twice"

    async def test_the_engine_never_clears_the_pollers_alarm(self, db):
        """The engine cannot see the queue. If it cleared what it did not find, every engine check would undo the poller."""
        config = _config()
        await andon.check_intake(db, config, waiting_cards=2)

        result = await andon.check_engine(db, config)

        assert result["cleared"] == []
        assert "line_idle_with_queue:intake" in await andon.load_raised(db, config, datetime.now(UTC))


# --------------------------------------------------------------------------
# Where it runs
# --------------------------------------------------------------------------


class TestPollerWiring:
    def _poller(self, db, config):
        from minions.providers.trello import LIST_ONDECK, TrelloPoller

        poller = TrelloPoller(config, db)
        poller._list_ids = {LIST_ONDECK: "ondeck", "in progress": "inprog", "done": "done", "fucked": "failed"}

        class _Resp:
            @staticmethod
            def raise_for_status():
                return None

            @staticmethod
            def json():
                return [{"id": "c1", "name": "queued", "desc": "", "labels": [{"name": "minion"}]}]

        calls = []

        async def _api(method, path, params=None):
            calls.append((method, path))
            return _Resp()

        poller._api = _api
        return poller, calls

    async def test_it_checks_even_when_intake_is_at_capacity(self, db, sample_job):
        """At capacity behind a stuck job is the state that stopped the line — and the state in which _poll returns early."""
        config = _config(max_concurrent_jobs=1, trello_min_job_interval=0, andon_idle_seconds=0)
        poller, calls = self._poller(db, config)

        await poller._poll()

        assert [e["detail"].split(" ")[0] for e in await _raised_events(db)] == ["line_idle_with_queue:intake"]
        assert not any(m == "PUT" for m, _ in calls), "launched a job past capacity"

    async def test_it_checks_on_its_own_cadence_not_every_poll(self, db, sample_job):
        config = _config(max_concurrent_jobs=1)
        poller, _ = self._poller(db, config)

        with patch("minions.andon.check_intake", AsyncMock(return_value={})) as check:
            await poller._poll()
            await poller._poll()

        assert check.await_count == 1

    async def test_a_failing_check_never_breaks_intake(self, db, sample_job):
        config = _config(max_concurrent_jobs=1)
        poller, _ = self._poller(db, config)

        with patch("minions.andon.check_intake", AsyncMock(side_effect=RuntimeError("boom"))):
            await poller._poll()


class TestMetricsGauge:
    async def test_raised_conditions_are_exposed_zero_included(self, db, sample_job, monkeypatch):
        import minions.dashboard as dash
        from tests.conftest import TEST_PG_URL

        monkeypatch.setattr(dash, "_postgres_url", TEST_PG_URL)
        config = _config()
        await andon.check_engine(db, config, _at(sample_job.updated_at, config.andon_stall_seconds + 60))

        payload = await dash._render_metrics()

        assert 'minion_andon_active{condition="stalled_job"} 1' in payload
        assert 'minion_andon_active{condition="stale_claim"} 0' in payload
        assert 'minion_andon_active{condition="line_idle_with_queue"} 0' in payload


class TestProductionDeclaresIt:
    def test_every_andon_setting_is_in_the_loaded_production_engine_table(self):
        """[production.engine] REPLACES [default.engine]. Read through the real loader, not the file text."""
        from minions import config as config_module

        engine = config_module._settings.from_env("production").get("engine") or {}
        declared = {key.lower() for key in engine}
        fields = {name for name in Config.__dataclass_fields__ if name.startswith("andon_")}

        assert fields, "no andon_* fields on Config"
        assert fields <= declared, f"missing from [production.engine]: {sorted(fields - declared)}"
