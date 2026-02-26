"""Persistent NATS client for review event publishing and subscription.

Carries forward the pattern from mcp-minions but scoped to review events.

Subjects:
    reviews.requested.<project>     — new MR needs review
    reviews.started.<project>       — engine picked it up
    reviews.completed.<project>     — review done (approve/request_changes)
    reviews.failed.<project>        — agent errored
    reviews.cost.<project>          — cost event for tracking
"""

import asyncio
import json
import logging
from typing import Callable, Optional

import nats
from nats.aio.client import Client as NatsConnection
from nats.aio.msg import Msg

from .nats_config import NatsConfig

logger = logging.getLogger(__name__)


class NatsClient:
    """Persistent NATS connection for review event pub/sub."""

    def __init__(self):
        self._nc: Optional[NatsConnection] = None
        self._subscriptions: list = []

    @property
    def is_connected(self) -> bool:
        return self._nc is not None and self._nc.is_connected

    async def connect(self, config: Optional[NatsConfig] = None) -> None:
        """Connect to NATS with auto-reconnect."""
        config = config or NatsConfig.from_env()

        connect_opts = {
            "servers": config.servers,
            "reconnect_time_wait": 2,
            "max_reconnect_attempts": -1,
        }
        if config.user and config.password:
            connect_opts["user"] = config.user
            connect_opts["password"] = config.password

        self._nc = await nats.connect(**connect_opts)
        servers_str = ", ".join(config.servers)
        logger.info("NatsClient connected to %s", servers_str)

    async def close(self) -> None:
        """Drain subscriptions and close the connection."""
        if self._nc and self._nc.is_connected:
            try:
                await self._nc.drain()
            except Exception:
                logger.debug("Error draining NATS connection", exc_info=True)
            try:
                await self._nc.close()
            except Exception:
                logger.debug("Error closing NATS connection", exc_info=True)
            logger.info("NatsClient closed")
        self._nc = None
        self._subscriptions = []

    async def publish(self, subject: str, payload: dict) -> None:
        """Fire-and-forget publish a JSON payload."""
        if not self._nc or not self._nc.is_connected:
            logger.warning("NatsClient not connected, skipping publish to %s", subject)
            return
        data = json.dumps(payload).encode("utf-8")
        await self._nc.publish(subject, data)
        logger.debug("Published to %s", subject)

    async def subscribe(self, subject: str, callback: Callable) -> None:
        """Subscribe to a subject with a message handler callback."""
        if not self._nc or not self._nc.is_connected:
            raise ConnectionError("NatsClient not connected")
        sub = await self._nc.subscribe(subject, cb=callback)
        self._subscriptions.append(sub)
        logger.info("Subscribed to %s", subject)

    # -- Convenience methods for review events --

    async def publish_review_requested(self, project: str, review_id: str, mr_url: str) -> None:
        await self.publish(f"reviews.requested.{project}", {
            "review_id": review_id,
            "mr_url": mr_url,
            "project": project,
        })

    async def publish_review_started(self, project: str, review_id: str) -> None:
        await self.publish(f"reviews.started.{project}", {
            "review_id": review_id,
            "project": project,
        })

    async def publish_review_completed(self, project: str, review_id: str, verdict: str, comments: int, cost: float) -> None:
        await self.publish(f"reviews.completed.{project}", {
            "review_id": review_id,
            "project": project,
            "verdict": verdict,
            "comments_posted": comments,
            "cost_usd": cost,
        })

    async def publish_review_failed(self, project: str, review_id: str, error: str) -> None:
        await self.publish(f"reviews.failed.{project}", {
            "review_id": review_id,
            "project": project,
            "error": error[:500],
        })

    @staticmethod
    async def reply(msg: Msg, payload: dict) -> None:
        """Respond to an incoming NATS request message."""
        data = json.dumps(payload).encode("utf-8")
        await msg.respond(data)
