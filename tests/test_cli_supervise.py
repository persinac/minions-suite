"""_supervise — making a dead background component visible.

Every long-running component in _run_pollers is an asyncio task that nothing
awaits; the parent blocks on an Event. An exception inside one is therefore
never retrieved, and the task list keeps a strong reference so it is never
garbage collected either — which means asyncio's own "Task exception was never
retrieved" warning never fires. The failure produced no output at all.

Observed live: the Trello poller raised out of _resolve_list_ids because the
board was missing a required list, and the pod logged "Input sources started:
trello" and then sat at 1/1 Running with nothing polling.
"""

import asyncio

import pytest

from minions.cli import _supervise


class TestSupervise:
    async def test_a_dying_task_trips_shutdown(self):
        shutdown = asyncio.Event()

        async def boom():
            raise RuntimeError("Missing required Trello lists: ['minions-on-deck']")

        _supervise(asyncio.create_task(boom(), name="trello-poller"), shutdown)
        await asyncio.wait_for(shutdown.wait(), timeout=2)

        assert shutdown.is_set()

    async def test_the_exception_is_retrieved_so_asyncio_stays_quiet(self):
        """Retrieving the exception is the point — an unretrieved one is silent."""
        shutdown = asyncio.Event()

        async def boom():
            raise RuntimeError("nope")

        task = asyncio.create_task(boom(), name="poller")
        _supervise(task, shutdown)
        await asyncio.wait_for(shutdown.wait(), timeout=2)

        assert task.exception() is not None

    async def test_a_task_returning_early_also_trips_shutdown(self):
        """A poller that returns is as broken as one that raises — it stops polling."""
        shutdown = asyncio.Event()

        async def finishes():
            return None

        _supervise(asyncio.create_task(finishes(), name="renovate-engine"), shutdown)
        await asyncio.wait_for(shutdown.wait(), timeout=2)

        assert shutdown.is_set()

    async def test_cancellation_does_not_trip_shutdown(self):
        """Shutdown cancels these tasks; that must not read as a component death."""
        shutdown = asyncio.Event()

        async def forever():
            await asyncio.Event().wait()

        task = asyncio.create_task(forever(), name="job-engine")
        _supervise(task, shutdown)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        await asyncio.sleep(0)
        assert not shutdown.is_set()

    async def test_a_healthy_task_leaves_shutdown_clear(self):
        shutdown = asyncio.Event()

        async def forever():
            await asyncio.Event().wait()

        task = _supervise(asyncio.create_task(forever(), name="trello-poller"), shutdown)
        await asyncio.sleep(0.05)

        assert not shutdown.is_set()

        task.cancel()

    async def test_logs_the_task_name_and_cause(self, caplog):
        """The operator needs to know which component died and why."""
        shutdown = asyncio.Event()

        async def boom():
            raise RuntimeError("Missing required Trello lists: ['minions-on-deck']")

        with caplog.at_level("ERROR"):
            _supervise(asyncio.create_task(boom(), name="trello-poller"), shutdown)
            await asyncio.wait_for(shutdown.wait(), timeout=2)

        text = caplog.text
        assert "trello-poller" in text
        assert "minions-on-deck" in text

    async def test_returns_the_task_so_it_can_be_tracked(self):
        """Callers append the result to `tasks` for cancellation on shutdown."""
        shutdown = asyncio.Event()

        async def forever():
            await asyncio.Event().wait()

        task = asyncio.create_task(forever(), name="x")
        assert _supervise(task, shutdown) is task

        task.cancel()
