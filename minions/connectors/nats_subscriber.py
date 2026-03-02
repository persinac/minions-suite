"""NATS JetStream subscriber for dashboard SSE endpoint.

Unlike the publisher (ephemeral), this maintains a persistent connection
to receive real-time events for streaming to the browser.
"""

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncGenerator

import nats
from nats.js.api import ConsumerConfig, DeliverPolicy

from .nats_config import NatsConfig

logger = logging.getLogger(__name__)


async def subscribe_events(subject: str = "system.events") -> AsyncGenerator[dict]:
    """Async generator that yields NATS messages from the given subject.

    Maintains a persistent JetStream push subscription. Handles reconnection
    on disconnect. Used by the dashboard SSE endpoint.

    Yields dicts parsed from the JSON message payload.
    """
    config = NatsConfig.from_env()

    connect_opts = {"servers": config.servers}
    if config.user and config.password:
        connect_opts["user"] = config.user
        connect_opts["password"] = config.password

    while True:
        nc = None
        try:
            nc = await nats.connect(**connect_opts)
            js = nc.jetstream()

            # Ephemeral push consumer, deliver only new messages
            sub = await js.subscribe(
                subject,
                config=ConsumerConfig(deliver_policy=DeliverPolicy.NEW),
            )

            logger.info(f"NATS subscriber connected, listening on {subject}")

            async for msg in sub.messages:
                try:
                    data = json.loads(msg.data.decode("utf-8"))
                    await msg.ack()
                    yield data
                except json.JSONDecodeError:
                    logger.warning(f"NATS subscriber received non-JSON message on {subject}")
                    await msg.ack()

        except asyncio.CancelledError:
            logger.info("NATS subscriber cancelled")
            if nc and nc.is_connected:
                await nc.close()
            return

        except Exception as e:
            logger.warning(f"NATS subscriber disconnected: {e}, reconnecting in 5s")
            if nc and nc.is_connected:
                with contextlib.suppress(Exception):
                    await nc.close()
            await asyncio.sleep(5)
