"""A full board and an empty queue must not look identical in the logs.

With the label gate on, on-deck is the team's backlog — never empty — so the
steady state is "26 cards, none eligible, poller does nothing". Every other
reason the poller might be idle (throttled, at max concurrency, crashed,
list-resolution failure) writes a log line. This one wrote none, so on
2026-07-28 the queue had been dead for days and the logs pointed everywhere
except the cause.

The warning has to be edge-triggered. _intake_interval_elapsed only throttles
once a job has actually started, so while nothing is eligible it returns True
every cycle and _get_cards runs at the raw 180s poll interval — an
unconditional warning would emit ~480 identical lines a day.
"""

import logging

import pytest

from minions.config import Config

pytestmark = pytest.mark.asyncio


def _poller():
    from unittest.mock import MagicMock

    from minions.providers.trello import TrelloPoller

    return TrelloPoller(Config.from_env(), MagicMock())


def _stub_cards(poller, monkeypatch, payload):
    class _Resp:
        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return payload

    async def _api(method, path, params=None):
        return _Resp()

    monkeypatch.setattr(poller, "_api", _api)


UNLABELLED = [
    {"id": "1", "name": "backlog item", "desc": "", "labels": []},
    {"id": "2", "name": "another", "desc": "", "labels": [{"name": "priority:high"}]},
]
LABELLED = [{"id": "3", "name": "queued", "desc": "", "labels": [{"name": "minion"}]}]


class TestTheIdleStateIsAnnounced:
    async def test_a_full_board_with_no_labels_warns(self, monkeypatch, caplog):
        poller = _poller()
        _stub_cards(poller, monkeypatch, UNLABELLED)

        with caplog.at_level(logging.WARNING, logger="minions.providers.trello"):
            cards = await poller._get_cards("list-id", require_minion_label=True)

        assert cards == []
        assert "intake is idle" in caplog.text
        # The count is the actionable part: it distinguishes "nobody labelled
        # anything" from "the board is genuinely empty".
        assert "2 card(s)" in caplog.text
        assert "minion" in caplog.text

    async def test_an_empty_board_does_not_warn(self, monkeypatch, caplog):
        """Nothing to label is not a misconfiguration — it is just done."""
        poller = _poller()
        _stub_cards(poller, monkeypatch, [])

        with caplog.at_level(logging.WARNING, logger="minions.providers.trello"):
            await poller._get_cards("list-id", require_minion_label=True)

        assert "intake is idle" not in caplog.text

    async def test_the_gate_being_off_never_warns(self, monkeypatch, caplog):
        poller = _poller()
        _stub_cards(poller, monkeypatch, UNLABELLED)

        with caplog.at_level(logging.WARNING, logger="minions.providers.trello"):
            cards = await poller._get_cards("list-id", require_minion_label=False)

        assert len(cards) == 2
        assert "intake is idle" not in caplog.text


class TestItIsEdgeTriggered:
    """The whole point of the flag. At a 180s poll interval an unconditional
    warning is ~480 lines a day describing one unchanging fact."""

    async def test_repeated_polls_warn_exactly_once(self, monkeypatch, caplog):
        poller = _poller()
        _stub_cards(poller, monkeypatch, UNLABELLED)

        with caplog.at_level(logging.WARNING, logger="minions.providers.trello"):
            for _ in range(25):
                await poller._get_cards("list-id", require_minion_label=True)

        assert caplog.text.count("intake is idle") == 1

    async def test_labelling_a_card_rearms_the_warning(self, monkeypatch, caplog):
        """Otherwise the second outage is silent — the failure mode this
        replaces, reintroduced one level down."""
        poller = _poller()

        with caplog.at_level(logging.INFO, logger="minions.providers.trello"):
            _stub_cards(poller, monkeypatch, UNLABELLED)
            await poller._get_cards("list-id", require_minion_label=True)

            _stub_cards(poller, monkeypatch, LABELLED)
            eligible = await poller._get_cards("list-id", require_minion_label=True)

            _stub_cards(poller, monkeypatch, UNLABELLED)
            await poller._get_cards("list-id", require_minion_label=True)

        assert [c["id"] for c in eligible] == ["3"]
        assert caplog.text.count("intake is idle") == 2
        assert "intake resumed" in caplog.text

    async def test_recovery_is_not_announced_when_nothing_was_wrong(self, monkeypatch, caplog):
        """ "Resumed" after a normal poll would imply an outage that never
        happened."""
        poller = _poller()
        _stub_cards(poller, monkeypatch, LABELLED)

        with caplog.at_level(logging.INFO, logger="minions.providers.trello"):
            await poller._get_cards("list-id", require_minion_label=True)

        assert "intake resumed" not in caplog.text
