"""_watch_pollers — catching the poller that hangs instead of dying.

_supervise (see test_cli_supervise.py) makes a *dead* component visible. It
cannot see the other way a poller stops working: the task stays alive, awaiting
something that never arrives. No exception is raised, no callback fires, and the
pod reports 1/1 Running with nothing being polled. Silence from a hung poller
reads exactly like silence from an empty queue.

The watchdog closes that gap by reading each poller's `last_poll_at` stamp and
tripping the same `shutdown` event a crash would, so a hang exits down the
existing non-zero path instead of idling forever.

A watchdog that is wired to nothing looks identical to one that works, so these
tests inject the fault and assert it fires — including through the real
`_run_pollers` wiring, where a watchdog that was never started would pass every
unit test above it.
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from minions import cli
from minions.cli import _find_stale_poller, _watch_pollers
from minions.config import Config

POLLERS = ["trello", "gitlab-issues"]

INTERVAL = 0.1
CHECK = 0.02
# The watchdog trips at INTERVAL * 3; wait well past that before calling it a
# failure, so a slow box does not read as a broken watchdog.
TRIP_TIMEOUT = 5


class _Stamped:
    """Minimal stand-in for a poller: an interval and a liveness stamp."""

    def __init__(self, interval, last_poll_at):
        self.poll_interval = interval
        self.last_poll_at = last_poll_at


async def _noop():
    return None


def _build(kind, monkeypatch, poll, interval=INTERVAL):
    """A real poller with only its network setup and one poll cycle stubbed."""
    config = Config.from_env()

    if kind == "trello":
        from minions.providers.trello import TrelloPoller

        config.trello_poll_interval = interval
        poller = TrelloPoller(config, MagicMock())
        setup = ("_resolve_list_ids", "_resolve_minion_label", "_rehydrate_active", "_reconcile_stranded_cards")
    else:
        from minions.providers.gitlab_issues import GitLabIssuesPoller

        config.gitlab_issues_poll_interval = interval
        poller = GitLabIssuesPoller(config, MagicMock(), {})
        setup = ("_rehydrate_active",)

    for name in setup:
        monkeypatch.setattr(poller, name, _noop)
    monkeypatch.setattr(poller, "_poll", poll)
    return poller


class TestStalenessRule:
    """The rule itself, pinned without waiting on a clock."""

    def test_a_fresh_stamp_is_not_stale(self):
        now = time.monotonic()
        pollers = [("trello-poller", _Stamped(INTERVAL, now))]

        assert _find_stale_poller(pollers, now) is None

    def test_a_stamp_older_than_three_intervals_is_stale(self):
        now = time.monotonic()
        pollers = [("trello-poller", _Stamped(60, now - 181))]

        stale = _find_stale_poller(pollers, now)

        assert stale is not None
        assert stale.name == "trello-poller"
        assert stale.interval == 60
        assert stale.deadline == 180
        assert stale.elapsed == pytest.approx(181)

    def test_exactly_at_the_deadline_is_not_yet_stale(self):
        """The boundary is `>`, so a poller landing on its deadline survives."""
        now = time.monotonic()
        pollers = [("trello-poller", _Stamped(60, now - 180))]

        assert _find_stale_poller(pollers, now) is None

    def test_the_multiplier_is_three(self):
        """N=3 is specified, not incidental — two intervals must not trip."""
        now = time.monotonic()
        pollers = [("trello-poller", _Stamped(60, now - 121))]

        assert _find_stale_poller(pollers, now) is None

    def test_a_stale_poller_is_found_among_healthy_ones(self):
        now = time.monotonic()
        pollers = [
            ("trello-poller", _Stamped(60, now)),
            ("gitlab-issues-poller", _Stamped(60, now - 300)),
        ]

        stale = _find_stale_poller(pollers, now)

        assert stale is not None
        assert stale.name == "gitlab-issues-poller"

    def test_a_poller_with_no_interval_is_not_judged(self):
        """No schedule means no deadline — not an instant failure."""
        now = time.monotonic()
        pollers = [("odd-poller", _Stamped(0, now - 10_000))]

        assert _find_stale_poller(pollers, now) is None


class TestWatchdogFires:
    @pytest.mark.parametrize("kind", POLLERS)
    async def test_a_hung_poller_trips_shutdown(self, kind, monkeypatch):
        """The case _supervise cannot reach: still alive, no longer working."""

        async def poll():
            await asyncio.Event().wait()

        poller = _build(kind, monkeypatch, poll)
        shutdown = asyncio.Event()

        poller_task = asyncio.create_task(poller.start())
        watchdog = asyncio.create_task(_watch_pollers([(kind, poller)], shutdown, CHECK, 3))
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=TRIP_TIMEOUT)
        finally:
            await poller.stop()
            poller_task.cancel()
            watchdog.cancel()

        assert shutdown.is_set()
        assert not poller_task.done(), "the poller never died — a crash-only check would have missed this"

    @pytest.mark.parametrize("kind", POLLERS)
    async def test_a_poller_that_raises_every_cycle_trips_shutdown(self, kind, monkeypatch):
        """Staleness is elapsed time, not cycle count — this poller is busy."""
        attempts = 0

        async def poll():
            nonlocal attempts
            attempts += 1
            raise RuntimeError("Trello API unreachable")

        poller = _build(kind, monkeypatch, poll)
        seeded = poller.last_poll_at
        shutdown = asyncio.Event()

        poller_task = asyncio.create_task(poller.start())
        watchdog = asyncio.create_task(_watch_pollers([(kind, poller)], shutdown, CHECK, 3))
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=TRIP_TIMEOUT)
        finally:
            await poller.stop()
            poller_task.cancel()
            watchdog.cancel()

        assert shutdown.is_set()
        assert attempts >= 2, "it kept trying the whole time"
        assert poller.last_poll_at == seeded, "and never once succeeded"

    @pytest.mark.parametrize("kind", POLLERS)
    async def test_a_healthy_poller_is_never_tripped(self, kind, monkeypatch):
        """Run well past the deadline: a false positive here restarts prod."""
        cycles = 0

        async def poll():
            nonlocal cycles
            cycles += 1

        poller = _build(kind, monkeypatch, poll)
        shutdown = asyncio.Event()

        poller_task = asyncio.create_task(poller.start())
        watchdog = asyncio.create_task(_watch_pollers([(kind, poller)], shutdown, CHECK, 3))
        try:
            # Nine intervals — three times the window that would trip a hang.
            await asyncio.sleep(INTERVAL * 9)
        finally:
            await poller.stop()
            poller_task.cancel()
            watchdog.cancel()

        assert not shutdown.is_set()
        assert cycles >= 5, "the poller really was cycling, so this was a fair test"

    async def test_the_log_names_the_poller_and_how_long_it_has_been_quiet(self, caplog):
        """After a restart, the log is the only way to tell a hang from a crash."""
        shutdown = asyncio.Event()
        stalled = _Stamped(INTERVAL, time.monotonic() - 999)

        with caplog.at_level("ERROR"):
            watchdog = asyncio.create_task(_watch_pollers([("trello-poller", stalled)], shutdown, CHECK, 3))
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=TRIP_TIMEOUT)
            finally:
                watchdog.cancel()

        assert "trello-poller" in caplog.text
        assert "hung" in caplog.text.lower()
        assert "999" in caplog.text

    async def test_the_watchdog_does_not_log_a_second_misleading_death(self, caplog):
        """It stays alive after tripping, so _supervise never overwrites the reason.

        Returning here would fire _supervise's done-callback and stack an
        "exited on its own" error on top of the real explanation.
        """
        shutdown = asyncio.Event()
        stalled = _Stamped(INTERVAL, time.monotonic() - 999)

        watchdog = cli._supervise(
            asyncio.create_task(_watch_pollers([("trello-poller", stalled)], shutdown, CHECK, 3), name="poller-watchdog"),
            shutdown,
        )
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=TRIP_TIMEOUT)
            await asyncio.sleep(CHECK * 3)
            assert not watchdog.done()
        finally:
            watchdog.cancel()

        assert "exited on its own" not in caplog.text


class TestCrashDetectionStillWorks:
    async def test_a_dying_poller_still_trips_shutdown_alongside_the_watchdog(self, monkeypatch):
        """The watchdog is additive: _supervise's path is untouched and faster."""

        async def dies():
            raise RuntimeError("Missing required Trello lists")

        healthy = _Stamped(INTERVAL, time.monotonic())
        shutdown = asyncio.Event()

        watchdog = asyncio.create_task(_watch_pollers([("trello-poller", healthy)], shutdown, CHECK, 3))
        cli._supervise(asyncio.create_task(dies(), name="trello-poller"), shutdown)
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=TRIP_TIMEOUT)
        finally:
            watchdog.cancel()

        assert shutdown.is_set()
        assert _find_stale_poller([("trello-poller", healthy)], time.monotonic()) is None, "the crash tripped it, not the watchdog"


class TestRunPollersWiring:
    """The watchdog has to be started and connected to the exit code.

    Every test above would still pass if _run_pollers never created the
    watchdog task — which is exactly the failure this feature exists to
    prevent, one layer up.
    """

    @pytest.mark.parametrize("kind", POLLERS)
    async def test_a_hung_poller_makes_the_process_exit_non_zero(self, kind, monkeypatch):
        class _Hung:
            poll_interval = INTERVAL

            def __init__(self, *args, **kwargs):
                self.last_poll_at = time.monotonic()

            async def start(self):
                await asyncio.Event().wait()

            async def stop(self):
                return None

        config = Config.from_env()
        config.engine_enabled = False
        config.nats_enabled = False
        config.k8s_dispatch = False

        if kind == "trello":
            config.gitlab_issues_enabled = False
            config.trello_api_key = "key"
            config.trello_token = "token"
            config.trello_board_id = "board"
            monkeypatch.setattr("minions.providers.trello.TrelloPoller", _Hung)
        else:
            config.trello_board_id = None
            config.gitlab_issues_enabled = True
            config.gitlab_token = "token"
            monkeypatch.setattr("minions.providers.gitlab_issues.GitLabIssuesPoller", _Hung)

        monkeypatch.setattr(cli, "POLLER_WATCHDOG_INTERVAL", CHECK)
        monkeypatch.setattr(cli, "_create_db", lambda cfg: AsyncMock())
        monkeypatch.setattr("minions.engine.JobEngine", MagicMock())
        monkeypatch.setattr("minions.artifact_uploader.ArtifactUploader", MagicMock())
        monkeypatch.setattr("minions.server.mcp.create_server", MagicMock())

        exit_code = await asyncio.wait_for(cli._run_pollers(config), timeout=TRIP_TIMEOUT)

        assert exit_code == 1
