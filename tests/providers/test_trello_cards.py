"""Filing a station's card: right lane, right labels, never the queue.

Runs the real httpx client against a MockTransport that plays the Trello API,
so the assertions are about the requests actually sent.
"""

import httpx
import pytest

from minions.config import Config
from minions.providers.trello_cards import QueueWriteRefused, file_card


class FakeTrello:
    def __init__(self, lists=None, labels=None):
        self.lists = lists if lists is not None else [{"id": "L-ondeck", "name": "On-deck"}, {"id": "L-inbox", "name": "Inbox"}]
        self.labels = labels if labels is not None else [{"id": "lab-minion", "name": "minion"}]
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path, method = request.url.path, request.method
        if method == "GET" and path.endswith("/lists"):
            return httpx.Response(200, json=self.lists)
        if method == "GET" and path.endswith("/labels"):
            return httpx.Response(200, json=self.labels)
        if method == "POST" and path.endswith("/1/lists"):
            new = {"id": "L-new", "name": request.url.params["name"]}
            self.lists.append(new)
            return httpx.Response(200, json=new)
        if method == "POST" and path.endswith("/1/labels"):
            new = {"id": f"lab-{request.url.params['name']}", "name": request.url.params["name"]}
            self.labels.append(new)
            return httpx.Response(200, json=new)
        if method == "POST" and path.endswith("/1/cards"):
            return httpx.Response(200, json={"id": "card-1", "shortUrl": "https://trello.com/c/abc"})
        return httpx.Response(404)

    def card_posts(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "POST" and r.url.path.endswith("/1/cards")]


def _config() -> Config:
    config = Config.from_env()
    config.trello_api_key = "k"
    config.trello_token = "t"
    config.trello_board_id = "board"
    return config


async def _file(fake: FakeTrello, list_name="Inbox", labels=("source:scout",)):
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)) as client:
        return await file_card(_config(), list_name, "title", "desc", list(labels), client=client)


@pytest.mark.asyncio
class TestFiling:
    async def test_files_into_the_named_lane_with_the_named_label(self):
        fake = FakeTrello()

        card = await _file(fake)

        assert card == {"card_id": "card-1", "url": "https://trello.com/c/abc"}
        post = fake.card_posts()[0]
        assert post.url.params["idList"] == "L-inbox"
        assert post.url.params["idLabels"] == "lab-source:scout"

    async def test_a_missing_lane_and_label_are_created_by_name(self):
        fake = FakeTrello(lists=[{"id": "L-ondeck", "name": "On-deck"}], labels=[])

        await _file(fake)

        assert fake.card_posts()[0].url.params["idList"] == "L-new"
        assert any(r.url.path.endswith("/1/labels") and r.method == "POST" for r in fake.requests)

    async def test_lane_names_match_case_insensitively(self):
        fake = FakeTrello(lists=[{"id": "L-inbox", "name": "  inbox "}])
        await _file(fake)
        assert fake.card_posts()[0].url.params["idList"] == "L-inbox"


@pytest.mark.asyncio
class TestTheQueueIsOffLimits:
    @pytest.mark.parametrize("lane", ["On-deck", "on-deck", "minions-on-deck", "In progress"])
    async def test_a_queue_lane_is_refused_before_any_request(self, lane):
        fake = FakeTrello()

        with pytest.raises(QueueWriteRefused):
            await _file(fake, list_name=lane)

        assert fake.requests == [], "a refused write must have no side effects"

    async def test_the_minion_label_is_refused_before_any_request(self):
        fake = FakeTrello()

        with pytest.raises(QueueWriteRefused):
            await _file(fake, labels=("source:scout", "Minion"))

        assert fake.requests == []
