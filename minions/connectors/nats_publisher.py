"""NATS JetStream publisher for agent messages and system events.

Ephemeral connect-publish-close pattern for fire-and-forget semantics.
Used by agent subprocesses that don't maintain a persistent NATS connection.
"""

import json
import logging
import os
from datetime import datetime, timezone

import nats

from .nats_config import NatsConfig

logger = logging.getLogger(__name__)


def _stream_name() -> str:
    """Return the JetStream stream name from env or default."""
    return os.getenv("JETSTREAM_STREAM", "minions")


async def _publish_async(subject: str, message: dict) -> bool:
    """Ephemeral connect -> JetStream publish -> close.

    Returns True on success, False on failure. Never raises.
    """
    config = NatsConfig.from_env()

    connect_opts = {"servers": config.servers}
    if config.user and config.password:
        connect_opts["user"] = config.user
        connect_opts["password"] = config.password

    nc = await nats.connect(**connect_opts)
    try:
        js = nc.jetstream()
        payload = json.dumps(message).encode("utf-8")
        ack = await js.publish(subject, payload)
        logger.info("NATS published to %s stream=%s seq=%s", subject, ack.stream, ack.seq)
        return True
    finally:
        await nc.close()


async def publish_agent_message(job_id: str, from_role: str, to_role: str | None, content: str) -> bool:
    """Publish an inter-agent message.

    Subject: agents.{job_id}.messages.{to_role or "broadcast"}
    """
    target = to_role if to_role else "broadcast"
    subject = f"agents.{job_id}.messages.{target}"

    message = {
        "job_id": job_id,
        "from_role": from_role,
        "to_role": to_role,
        "content": content,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    try:
        return await _publish_async(subject, message)
    except Exception as e:
        logger.error("NATS publish_agent_message failed: %s", e)
        return False


async def publish_agent_status(job_id: str, agent_id: str, role: str, status: str) -> bool:
    """Publish an agent lifecycle status update.

    Subject: agents.{job_id}.status
    """
    subject = f"agents.{job_id}.status"

    message = {
        "job_id": job_id,
        "agent_id": agent_id,
        "role": role,
        "status": status,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    try:
        return await _publish_async(subject, message)
    except Exception as e:
        logger.error("NATS publish_agent_status failed: %s", e)
        return False


async def publish_system_event(job_id: str | None, event_type: str, source: str, detail: str | None = None) -> bool:
    """Publish a system-wide event for the dashboard SSE stream.

    Subject: system.events
    """
    subject = "system.events"

    message = {
        "job_id": job_id,
        "event_type": event_type,
        "source": source,
        "detail": detail,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    try:
        return await _publish_async(subject, message)
    except Exception as e:
        logger.error("NATS publish_system_event failed: %s", e)
        return False
