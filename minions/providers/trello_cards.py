"""File a card on the board in a named lane, with named labels.

Used by stations that create work (scout first). The lane and labels are
resolved BY NAME and created if absent, so turning a station on does not need a
human to prepare the board first.

What a station may never do from here is queue work. A card in On-deck that
carries the `minion` label is claimable by the line, and the only thing allowed
to decide that is the gate in front of the line -- the groomer today, the weight
station once it exists (openspec/changes/factory-stations/design.md). So the
queue lanes and the `minion` label are refused outright, whatever the caller
passes. That is a hard boundary, not a convention: a station that "just this
once" queued its own card would be a station grading its own homework.
"""

import logging

import httpx

logger = logging.getLogger(__name__)

TRELLO_API = "https://api.trello.com/1"

# Lanes the line reads from. Never a station's target. Matched case-insensitively.
QUEUE_LANES: frozenset[str] = frozenset({"on-deck", "minions-on-deck", "in progress"})

# The label the Trello poller requires before it will pick a card up.
QUEUE_LABEL = "minion"


class QueueWriteRefused(ValueError):
    """A station tried to put work directly in front of the line."""


async def _find_or_create_list(client: httpx.AsyncClient, auth: dict, board_id: str, name: str) -> str:
    resp = await client.get(f"{TRELLO_API}/boards/{board_id}/lists", params={**auth, "fields": "name"})
    resp.raise_for_status()
    for lst in resp.json():
        if lst.get("name", "").strip().lower() == name.lower():
            return lst["id"]
    resp = await client.post(f"{TRELLO_API}/lists", params={**auth, "name": name, "idBoard": board_id, "pos": "top"})
    resp.raise_for_status()
    logger.info("Created Trello list %r", name)
    return resp.json()["id"]


async def _find_or_create_labels(client: httpx.AsyncClient, auth: dict, board_id: str, names: list[str]) -> list[str]:
    resp = await client.get(f"{TRELLO_API}/boards/{board_id}/labels", params={**auth, "fields": "name", "limit": 1000})
    resp.raise_for_status()
    existing = {(label.get("name") or "").strip().lower(): label["id"] for label in resp.json()}
    ids = []
    for name in names:
        label_id = existing.get(name.lower())
        if label_id is None:
            resp = await client.post(f"{TRELLO_API}/labels", params={**auth, "name": name, "color": "sky", "idBoard": board_id})
            resp.raise_for_status()
            label_id = resp.json()["id"]
            logger.info("Created Trello label %r", name)
        ids.append(label_id)
    return ids


async def file_card(
    config,
    list_name: str,
    title: str,
    description: str,
    labels: list[str],
    client: httpx.AsyncClient | None = None,
) -> dict:
    """Create a card. Returns {"card_id", "url"}. Raises on HTTP failure.

    Refuses any queue lane and the `minion` label before touching the network,
    so a refused call has no side effects to undo.
    """
    if list_name.strip().lower() in QUEUE_LANES:
        raise QueueWriteRefused(f"A station may not file into {list_name!r}: that lane feeds the line. File into an intake lane instead.")
    if any(label.strip().lower() == QUEUE_LABEL for label in labels):
        raise QueueWriteRefused(f"A station may not apply the {QUEUE_LABEL!r} label: that is what makes a card claimable by the line.")
    if not (config.trello_api_key and config.trello_token and config.trello_board_id):
        raise RuntimeError("Trello credentials are not configured (TRELLO_API_KEY, TRELLO_TOKEN, TRELLO_BOARD_ID)")

    auth = {"key": config.trello_api_key, "token": config.trello_token}
    owned = client is None
    if owned:
        client = httpx.AsyncClient(timeout=30.0)
    try:
        list_id = await _find_or_create_list(client, auth, config.trello_board_id, list_name)
        label_ids = await _find_or_create_labels(client, auth, config.trello_board_id, labels)
        params = {**auth, "idList": list_id, "name": title, "desc": description, "pos": "top"}
        if label_ids:
            params["idLabels"] = ",".join(label_ids)
        resp = await client.post(f"{TRELLO_API}/cards", params=params)
        resp.raise_for_status()
        card = resp.json()
        return {"card_id": card["id"], "url": card.get("shortUrl") or card.get("url", "")}
    finally:
        if owned:
            await client.aclose()
