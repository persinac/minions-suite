"""Ensure JetStream stream and consumers exist on startup.

Idempotent — safe to run every time the server starts.
Mirrors the stream config from mcp-minions/scripts/init_nats.py.
"""

import logging
import os

from nats.js.api import (
    ConsumerConfig,
    DiscardPolicy,
    RetentionPolicy,
    StorageType,
    StreamConfig,
)
from nats.js.errors import NotFoundError

from .nats_client import NatsClient

logger = logging.getLogger(__name__)

STREAM_NAME = os.getenv("JETSTREAM_STREAM", "minions")
STREAM_SUBJECTS = [
    "agents.>",
    "system.>",
    "reviews.>",
    "jobs.>",
]
STREAM_DESCRIPTION = "MCP Minions agent messaging, review events, and system events"
MAX_MSGS = 1_000_000
MAX_AGE = 7 * 24 * 3600  # 7 days in seconds

WORK_CONSUMER_NAME = "agent-workers"
WORK_CONSUMER_SUBJECT = "agents.work"
WORK_ACK_WAIT = 120
WORK_MAX_DELIVER = 3
WORK_MAX_ACK_PENDING = 10


async def ensure_jetstream_stream(nats_client: NatsClient) -> None:
    """Create or update the JetStream stream and durable consumer.

    Called on every server startup. No-ops if everything is already current.
    """
    js = nats_client._js

    stream_cfg = StreamConfig(
        name=STREAM_NAME,
        subjects=STREAM_SUBJECTS,
        retention=RetentionPolicy.LIMITS,
        storage=StorageType.FILE,
        max_msgs=MAX_MSGS,
        max_age=MAX_AGE,
        max_msg_size=1024 * 1024,
        discard=DiscardPolicy.OLD,
        description=STREAM_DESCRIPTION,
        num_replicas=1,
    )

    try:
        await js.stream_info(STREAM_NAME)
        await js.update_stream(stream_cfg)
        logger.info("JetStream stream '%s' verified (updated)", STREAM_NAME)
    except NotFoundError:
        await js.add_stream(stream_cfg)
        logger.info("JetStream stream '%s' created", STREAM_NAME)

    consumer_cfg = ConsumerConfig(
        durable_name=WORK_CONSUMER_NAME,
        filter_subject=WORK_CONSUMER_SUBJECT,
        ack_wait=WORK_ACK_WAIT,
        max_deliver=WORK_MAX_DELIVER,
        max_ack_pending=WORK_MAX_ACK_PENDING,
    )

    try:
        await js.consumer_info(STREAM_NAME, WORK_CONSUMER_NAME)
        logger.info("JetStream consumer '%s' verified", WORK_CONSUMER_NAME)
    except NotFoundError:
        await js.add_consumer(STREAM_NAME, consumer_cfg)
        logger.info("JetStream consumer '%s' created", WORK_CONSUMER_NAME)
