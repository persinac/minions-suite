"""Persistent NATS client for the minions-suite.

Uses the shared 'minions' JetStream stream (same as mcp-minions).
All subjects are captured by the single stream.

Subjects:
    jobs.review.requested.<project> — new review job created
    jobs.review.started.<project>   — review job picked up
    jobs.review.completed.<project> — review job done (approve/request_changes)
    jobs.review.failed.<project>    — review job errored
    agents.work                     — job work items (K8s dispatch)
    agents.<job_id>.status          — agent lifecycle events
    agents.results.<job_id>         — agent completion results
    jobs.<job_id>.status            — job status updates
    system.events                   — dashboard SSE events
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

    # -- Convenience methods for review job events --

    async def publish_review_requested(self, project: str, job_id: str, mr_url: str) -> None:
        await self.publish(f"jobs.review.requested.{project}", {
            "job_id": job_id,
            "mr_url": mr_url,
            "project": project,
        })

    async def publish_review_started(self, project: str, job_id: str) -> None:
        await self.publish(f"jobs.review.started.{project}", {
            "job_id": job_id,
            "project": project,
        })

    async def publish_review_completed(self, project: str, job_id: str, verdict: str, comments: int, cost: float) -> None:
        await self.publish(f"jobs.review.completed.{project}", {
            "job_id": job_id,
            "project": project,
            "verdict": verdict,
            "comments_posted": comments,
            "cost_usd": cost,
        })

    async def publish_review_failed(self, project: str, job_id: str, error: str) -> None:
        await self.publish(f"jobs.review.failed.{project}", {
            "job_id": job_id,
            "project": project,
            "error": error[:500],
        })

    @staticmethod
    async def reply(msg: Msg, payload: dict) -> None:
        """Respond to an incoming NATS request message."""
        data = json.dumps(payload).encode("utf-8")
        await msg.respond(data)

    # -- JetStream support for job orchestration --

    @property
    def _js(self):
        """Get JetStream context from the current connection."""
        if not self._nc or not self._nc.is_connected:
            raise ConnectionError("NatsClient not connected")
        return self._nc.jetstream()

    async def request(self, subject: str, payload: dict, timeout: float = 10.0) -> dict:
        """NATS request/reply for arbiter state transitions.

        Returns the parsed JSON response.
        """
        if not self._nc or not self._nc.is_connected:
            raise ConnectionError("NatsClient not connected")
        data = json.dumps(payload).encode("utf-8")
        response = await self._nc.request(subject, data, timeout=timeout)
        return json.loads(response.data.decode("utf-8"))

    async def publish_work(self, subject: str, data: dict) -> None:
        """Publish a work item to JetStream durable work queue."""
        js = self._js
        payload = json.dumps(data).encode("utf-8")
        ack = await js.publish(subject, payload)
        logger.debug("JetStream published to %s stream=%s seq=%s", subject, ack.stream, ack.seq)

    async def subscribe_work_queue(self, handler: Callable, consumer_name: str = "agent-workers") -> None:
        """Subscribe to durable pull subscription on agents.work queue."""
        js = self._js
        sub = await js.subscribe("agents.work", cb=handler, durable=consumer_name)
        self._subscriptions.append(sub)
        logger.info("JetStream subscribed to agents.work (consumer=%s)", consumer_name)

    async def subscribe_results(self, handler: Callable) -> None:
        """Subscribe to agent result messages on agents.results.>"""
        js = self._js
        sub = await js.subscribe("agents.results.>", cb=handler)
        self._subscriptions.append(sub)
        logger.info("JetStream subscribed to agents.results.>")

    @staticmethod
    async def extend_ack(msg: Msg) -> None:
        """Send in-progress ack for long-running work items."""
        await msg.in_progress()

    # -- Job event convenience methods --

    async def publish_job_status(self, job_id: str, status: str, detail: Optional[str] = None) -> None:
        await self.publish(f"jobs.{job_id}.status", {
            "job_id": job_id,
            "status": status,
            "detail": detail,
        })

    async def publish_agent_status(self, job_id: str, agent_id: str, role: str, status: str) -> None:
        await self.publish(f"agents.{job_id}.status", {
            "job_id": job_id,
            "agent_id": agent_id,
            "role": role,
            "status": status,
        })

    async def publish_system_event(self, job_id: Optional[str], event_type: str, source: str, detail: Optional[str] = None) -> None:
        await self.publish("system.events", {
            "job_id": job_id,
            "event_type": event_type,
            "source": source,
            "detail": detail,
        })
