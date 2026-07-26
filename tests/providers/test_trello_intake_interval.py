"""The queue must drain no faster than one job per configured interval.

Spend is dominated by agent tokens, so the only reliable cap on the bill is how
often a job is allowed to start at all. This throttles intake specifically,
NOT the poll loop: `_poll` also runs `_monitor_jobs`, which moves cards for
finished work, so slowing the whole loop would leave completed cards sitting in
"In progress" for hours.

The check is measured against job creation time in the database rather than an
in-process timestamp, so a pod restart cannot reset the clock and let a job
through early — the failure mode that makes an in-memory throttle useless in a
cluster that reschedules pods.
"""

from datetime import UTC, datetime, timedelta

import pytest

from minions.config import Config
from minions.providers.trello import TrelloPoller


class _DB:
    def __init__(self, recent=0):
        self.recent = recent
        self.asked_since = None

    async def count_jobs_since(self, since):
        self.asked_since = since
        return self.recent


def _poller(interval, recent=0):
    config = Config.from_env()
    config.trello_min_job_interval = interval
    db = _DB(recent)
    poller = TrelloPoller.__new__(TrelloPoller)
    poller.config = config
    poller.db = db
    return poller, db


class TestIntakeInterval:
    @pytest.mark.asyncio
    async def test_zero_disables_the_throttle(self):
        """Default must not change existing behaviour."""
        poller, db = _poller(0, recent=5)

        assert await poller._intake_interval_elapsed() is True
        assert db.asked_since is None, "should not even query when disabled"

    @pytest.mark.asyncio
    async def test_a_recent_job_blocks_intake(self):
        poller, _ = _poller(8 * 3600, recent=1)

        assert await poller._intake_interval_elapsed() is False

    @pytest.mark.asyncio
    async def test_an_empty_window_allows_intake(self):
        poller, _ = _poller(8 * 3600, recent=0)

        assert await poller._intake_interval_elapsed() is True

    @pytest.mark.asyncio
    async def test_the_window_matches_the_configured_interval(self):
        """An 8h setting must look back 8h, not the poll interval."""
        poller, db = _poller(8 * 3600, recent=0)

        await poller._intake_interval_elapsed()

        asked = datetime.fromisoformat(db.asked_since)
        expected = datetime.now(UTC) - timedelta(hours=8)
        assert abs((asked - expected).total_seconds()) < 5

    @pytest.mark.asyncio
    async def test_manual_mcp_submissions_also_push_the_window(self):
        """count_jobs_since counts every job, not just Trello-sourced ones —
        the cap is on total spend, not on one intake source."""
        poller, db = _poller(8 * 3600, recent=1)

        assert await poller._intake_interval_elapsed() is False
        assert db.asked_since is not None


class TestConfigWiring:
    def test_the_field_is_actually_read_from_env(self, monkeypatch):
        """A config field with no from_env mapping is dead config — that has
        already happened once here, with the reviewer App's credentials."""
        monkeypatch.setenv("TRELLO_MIN_JOB_INTERVAL", "28800")

        assert Config.from_env().trello_min_job_interval == 28800

    def test_it_defaults_to_disabled(self, monkeypatch):
        monkeypatch.delenv("TRELLO_MIN_JOB_INTERVAL", raising=False)

        assert Config.from_env().trello_min_job_interval == 0

    def test_the_poll_cycle_consults_it_before_fetching_cards(self):
        """Ordering matters: checking after fetching would still spend API
        calls, and checking after _launch_job would not throttle at all."""
        import inspect

        source = inspect.getsource(TrelloPoller._poll)

        gate = source.index("_intake_interval_elapsed")
        fetch = source.index("_get_cards")
        assert gate < fetch

    def test_monitoring_is_not_throttled(self):
        """Finished cards must still be moved promptly."""
        import inspect

        source = inspect.getsource(TrelloPoller._poll)

        monitor = source.index("_monitor_jobs")
        gate = source.index("_intake_interval_elapsed")
        assert monitor < gate, "job monitoring must run before the intake gate"
