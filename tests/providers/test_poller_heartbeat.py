"""last_poll_at — the stamp that tells a working poller from a stuck one.

The watchdog in cli.py can only be as honest as this stamp. If it advanced at
the *top* of a cycle, or from a `finally`, then a poller whose every cycle
raises would keep stamping and look perfectly healthy right up until someone
noticed no cards had moved in a day. So the stamp is written on exactly one
line: immediately after `await self._poll()` returns, inside the `try`.

That distinction is invisible by reading — both versions are one assignment in
the same loop — so it is pinned here by fault injection instead: drive the real
`start()` loop with a `_poll` that fails, and assert the stamp never moved.
"""

import asyncio
from unittest.mock import MagicMock

import httpx
import pytest

from minions.config import Config

pytestmark = pytest.mark.asyncio

POLLERS = ["trello", "gitlab-issues"]

# Short enough to get several cycles inside a test, long enough that a loaded
# CI box does not turn scheduling jitter into a false failure.
INTERVAL = 0.1


async def _noop():
    return None


def _build(kind, monkeypatch, poll, interval=INTERVAL):
    """A real poller, with only its network setup and one poll cycle stubbed.

    The loop under test is the real one — stubbing `start()` itself would test
    nothing, since the stamp lives in that loop.
    """
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


async def _run_for(poller, seconds):
    """Run the real polling loop for a while, then shut it down cleanly."""
    task = asyncio.create_task(poller.start())
    try:
        await asyncio.sleep(seconds)
    finally:
        await poller.stop()
        task.cancel()
    return task


class TestHeartbeatStamp:
    @pytest.mark.parametrize("kind", POLLERS)
    async def test_the_stamp_is_seeded_at_construction(self, kind, monkeypatch):
        """Without a seed the first window has no baseline to measure from."""
        poller = _build(kind, monkeypatch, _noop)

        assert isinstance(poller.last_poll_at, float)

    @pytest.mark.parametrize("kind", POLLERS)
    async def test_poll_interval_is_exposed_under_one_name(self, kind, monkeypatch):
        """Each poller has its own config key; the watchdog reads one name."""
        poller = _build(kind, monkeypatch, _noop, interval=7)

        assert poller.poll_interval == 7

    @pytest.mark.parametrize("kind", POLLERS)
    async def test_a_completed_cycle_advances_the_stamp(self, kind, monkeypatch):
        cycles = 0

        async def poll():
            nonlocal cycles
            cycles += 1

        poller = _build(kind, monkeypatch, poll)
        seeded = poller.last_poll_at

        await _run_for(poller, INTERVAL * 3)

        assert cycles >= 2
        assert poller.last_poll_at > seeded

    @pytest.mark.parametrize("kind", POLLERS)
    @pytest.mark.parametrize("error", [httpx.HTTPError("Trello API unreachable"), RuntimeError("bad list id")])
    async def test_a_cycle_that_raises_never_advances_the_stamp(self, kind, error, monkeypatch):
        """The load-bearing assertion of this whole feature.

        Both `except` branches in the loop are covered, because a stamp written
        into only one of them would still let the other path fake liveness.
        """
        attempts = 0

        async def poll():
            nonlocal attempts
            attempts += 1
            raise error

        poller = _build(kind, monkeypatch, poll)
        seeded = poller.last_poll_at

        await _run_for(poller, INTERVAL * 3)

        assert attempts >= 2, "the loop should keep trying — failing is not stopping"
        assert poller.last_poll_at == seeded, "a failing poller must never claim a successful poll"

    @pytest.mark.parametrize("kind", POLLERS)
    async def test_a_hung_cycle_never_advances_the_stamp(self, kind, monkeypatch):
        """The hang case: alive, awaiting forever, and therefore never stamping."""

        async def poll():
            await asyncio.Event().wait()

        poller = _build(kind, monkeypatch, poll)
        seeded = poller.last_poll_at

        task = await _run_for(poller, INTERVAL * 3)

        assert poller.last_poll_at == seeded
        assert not task.done(), "a hung poller does not die — that is why _supervise cannot see it"
