"""Slack webhook notifications — best-effort, never in the pipeline's way.

Before this existed, a job submitted over MCP or the CLI could fail and the
only trace was a database row: Trello-sourced jobs got a card comment,
GitLab-issue jobs got an issue comment, and everything else was silent until
someone opened the dashboard. The pickup and completion DMs Alex actually
read were being relayed by hand from a log monitor.

An incoming webhook's destination is baked into its URL when Slack issues
it, so this module needs no channel configuration: set SLACK_WEBHOOK_URL
(a secret — env only, never settings.toml) to a webhook pointed wherever
the messages should land, and every process that loads Config can send.
Empty URL means the feature is off and every call is a cheap no-op.

Delivery is best-effort by design. A notification is about the pipeline;
it must never become part of it — a Slack outage that failed jobs would
invert the point entirely. Failures log a warning and return False.

Message style per the house rule for third-party media: short sentences,
plain words, the id and the outcome first.
"""

import logging

import httpx

from .core.models import Job, Task

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 5.0


_CHAT_POST_MESSAGE = "https://slack.com/api/chat.postMessage"


async def notify(webhook_url: str, message: str, *, bot_token: str = "", target: str = "") -> bool:
    """Send one notification; bot token wins over webhook. Nothing set = no-op, returns False."""
    if bot_token and target:
        return await _post_as_bot(bot_token, target, message)
    if webhook_url:
        return await _post_webhook(webhook_url, message)
    return False


async def _post_webhook(webhook_url: str, message: str) -> bool:
    """POST one message to a Slack incoming webhook."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            response = await client.post(webhook_url, json={"text": message})
        if response.status_code >= 300:
            logger.warning("Slack notify failed (HTTP %s): %s", response.status_code, response.text[:120])
            return False
        return True
    except Exception as e:
        logger.warning("Slack notify failed: %s", e)
        return False


async def _post_as_bot(bot_token: str, target: str, message: str) -> bool:
    """POST via chat.postMessage. True only on {"ok": true}: Slack refuses with HTTP 200."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            response = await client.post(
                _CHAT_POST_MESSAGE,
                json={"channel": target, "text": message},
                headers={"Authorization": f"Bearer {bot_token}"},
            )
        if response.status_code >= 300:
            logger.warning("Slack chat.postMessage failed (HTTP %s): %s", response.status_code, response.text[:120])
            return False
        body = response.json()
        if not body.get("ok"):
            # Never log the token; the error code is the whole diagnosis.
            logger.warning("Slack chat.postMessage refused for %s: %s", target, str(body.get("error"))[:120])
            return False
        return True
    except Exception as e:
        logger.warning("Slack chat.postMessage failed: %s", e)
        return False


def _title(job: Job) -> str:
    first_line = (job.spec or "").strip().splitlines()[0] if (job.spec or "").strip() else ""
    return first_line.lstrip("# ").strip()[:90]


def pickup_message(job: Job) -> str:
    difficulty = job.difficulty or "unclassified"
    return f":robot_face: Minion pickup: job `{job.id}` (*{difficulty}*) — {_title(job)}"


def terminal_message(job: Job, tasks: list[Task], cost_usd: float) -> str:
    status = str(job.status)
    if status == "done":
        icon = ":white_check_mark:"
    elif status == "no_work_needed":
        icon = ":ok_hand:"
    else:
        icon = ":warning:"

    lines = [f"{icon} Job `{job.id}` *{status}* — {_title(job)}"]

    prs = sorted({t.pr_url for t in tasks if t.pr_url})
    for pr in prs:
        lines.append(f"• PR: {pr}")
    if status == "failed" and job.error:
        lines.append(f"• Error: {str(job.error)[:150]}")
    lines.append(f"• Metered cost: ${cost_usd:.2f}")
    return "\n".join(lines)
