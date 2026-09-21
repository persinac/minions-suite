"""Notifications are best-effort, deduped, and never part of the pipeline.

A job submitted over MCP or the CLI used to fail into a database row and
nothing else. The webhook DMs replace a human relaying pickups and outcomes
by hand — and because they are about the pipeline, they must never become
part of it: an unset URL is a no-op, a Slack outage is a logged warning,
and no failure here may gate an analyst launch or terminal bookkeeping.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from minions.core.models import AgentRole, JobStatus, Task, TaskStatus
from minions.notify import notify, pickup_message, terminal_message

WEBHOOK = "https://hooks.slack.com/services/T000/B000/XXX"


def _http(status_code=200, raises=None):
    calls = []

    class _Response:
        def __init__(self):
            self.status_code = status_code
            self.text = "ng" if status_code >= 300 else "ok"

    class _Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            if raises:
                raise raises
            calls.append((url, json))
            return _Response()

    return _Client, calls


class TestNotifyIsBestEffort:
    async def test_an_empty_url_is_a_cheap_no_op(self):
        client, calls = _http()
        with patch("minions.notify.httpx.AsyncClient", client):
            assert await notify("", "hello") is False
        assert not calls, "no URL, no HTTP"

    async def test_a_message_posts_as_slack_text_json(self):
        client, calls = _http()
        with patch("minions.notify.httpx.AsyncClient", client):
            assert await notify(WEBHOOK, "hello") is True
        assert calls == [(WEBHOOK, {"text": "hello"})]

    async def test_an_http_error_is_false_not_raised(self):
        client, _ = _http(status_code=500)
        with patch("minions.notify.httpx.AsyncClient", client):
            assert await notify(WEBHOOK, "hello") is False

    async def test_a_network_failure_is_false_not_raised(self):
        client, _ = _http(raises=OSError("connection refused"))
        with patch("minions.notify.httpx.AsyncClient", client):
            assert await notify(WEBHOOK, "hello") is False


BOT_TOKEN = "xoxb-not-a-real-token"
DM_TARGET = "U01QYVD8UTD"
CHAT_URL = "https://slack.com/api/chat.postMessage"


def _bot_http(status_code=200, body=None, raises=None):
    """Like _http, but records headers and returns a body worth judging."""
    seen = []

    class _Response:
        def __init__(self):
            self.status_code = status_code
            self.text = "ng" if status_code >= 300 else "ok"

        def json(self):
            return {"ok": True} if body is None else body

    class _Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            if raises:
                raise raises
            seen.append({"url": url, "json": json, "headers": headers or {}})
            return _Response()

    return _Client, seen


class TestTheBotPath:
    """A webhook cannot DM; Workflow Builder can but is paid-plan only."""

    async def test_a_bot_token_and_target_dm_via_chat_post_message(self):
        client, seen = _bot_http()
        with patch("minions.notify.httpx.AsyncClient", client):
            assert await notify("", "hello", bot_token=BOT_TOKEN, target=DM_TARGET) is True
        assert seen[0]["url"] == CHAT_URL
        assert seen[0]["json"] == {"channel": DM_TARGET, "text": "hello"}
        assert seen[0]["headers"]["Authorization"] == f"Bearer {BOT_TOKEN}"

    async def test_the_bot_wins_when_both_are_configured(self):
        client, seen = _bot_http()
        with patch("minions.notify.httpx.AsyncClient", client):
            assert await notify(WEBHOOK, "hello", bot_token=BOT_TOKEN, target=DM_TARGET) is True
        assert seen[0]["url"] == CHAT_URL, "a configured bot must not fall back to the channel-only webhook"

    async def test_a_token_without_a_target_falls_back_to_the_webhook(self):
        """Half-configured must not send nowhere while a working webhook sits there."""
        client, calls = _http()
        with patch("minions.notify.httpx.AsyncClient", client):
            assert await notify(WEBHOOK, "hello", bot_token=BOT_TOKEN) is True
        assert calls == [(WEBHOOK, {"text": "hello"})]

    async def test_nothing_configured_is_still_a_no_op(self):
        client, seen = _bot_http()
        with patch("minions.notify.httpx.AsyncClient", client):
            assert await notify("", "hello", bot_token="", target="") is False
        assert not seen

    async def test_a_refusal_at_http_200_is_false(self):
        """Slack refuses with HTTP 200; reading the status code would call it delivered."""
        client, _ = _bot_http(status_code=200, body={"ok": False, "error": "channel_not_found"})
        with patch("minions.notify.httpx.AsyncClient", client):
            assert await notify("", "hello", bot_token=BOT_TOKEN, target=DM_TARGET) is False

    async def test_a_bot_network_failure_is_false_not_raised(self):
        client, _ = _bot_http(raises=OSError("connection refused"))
        with patch("minions.notify.httpx.AsyncClient", client):
            assert await notify("", "hello", bot_token=BOT_TOKEN, target=DM_TARGET) is False

    def test_the_call_sites_guard_on_slack_enabled_not_the_webhook(self):
        """Both notify call sites are behind a config check. Gate on the webhook
        alone and a bot-only setup sends nothing while looking configured."""
        from minions.config import Config

        cfg = Config.from_env()
        cfg.slack_webhook_url = ""
        cfg.slack_bot_token = BOT_TOKEN
        cfg.slack_dm_target = DM_TARGET
        assert cfg.slack_enabled is True

        cfg.slack_dm_target = ""
        assert cfg.slack_enabled is False, "a token with nowhere to post is not configured"

        cfg.slack_bot_token = ""
        cfg.slack_webhook_url = WEBHOOK
        assert cfg.slack_enabled is True

        cfg.slack_webhook_url = ""
        assert cfg.slack_enabled is False


def _job(status=JobStatus.DONE, spec="# Fix the flux capacitor\n\ndetails", difficulty="medium", error=None):
    job = MagicMock()
    job.id = "abcd1234"
    job.status = status
    job.spec = spec
    job.difficulty = difficulty
    job.error = error
    return job


class TestMessages:
    def test_pickup_leads_with_id_difficulty_and_title(self):
        out = pickup_message(_job())

        assert "`abcd1234`" in out
        assert "medium" in out
        assert "Fix the flux capacitor" in out
        assert "#" not in out.split("—")[1], "the markdown heading marker is stripped from the title"

    def test_pickup_survives_an_unclassified_job(self):
        assert "unclassified" in pickup_message(_job(difficulty=None))

    def test_terminal_done_carries_the_pr_and_the_cost(self):
        task = Task(job_id="abcd1234", title="t", description="d", service="api", agent_role=AgentRole.BACKEND_ENGINEER)
        task.pr_url = "https://github.com/o/r/pull/9"
        task.status = TaskStatus.DONE

        out = terminal_message(_job(), [task], 2.33)

        assert "done" in out
        assert "https://github.com/o/r/pull/9" in out
        assert "$2.33" in out

    def test_terminal_failed_carries_the_error(self):
        out = terminal_message(_job(status=JobStatus.FAILED, error="All dev tasks failed"), [], 1.07)

        assert ":warning:" in out
        assert "All dev tasks failed" in out

    def test_terminal_dedupes_pr_urls_across_tasks(self):
        t1 = Task(job_id="j", title="t", description="d", service="api", agent_role=AgentRole.BACKEND_ENGINEER)
        t2 = Task(job_id="j", title="t2", description="d", service="api", agent_role=AgentRole.BACKEND_ENGINEER)
        t1.pr_url = t2.pr_url = "https://github.com/o/r/pull/9"

        out = terminal_message(_job(), [t1, t2], 0.5)

        assert out.count("pull/9") == 1


class TestTerminalHookDedupes:
    async def test_on_job_terminal_sends_once(self, db):
        """Failure paths can reach _on_job_terminal more than once; the DM
        must not follow suit — two DMs for one job reads like two jobs."""
        from minions.engine.job_engine import JobEngine

        job = await db.create_job("# a job")
        engine = MagicMock(spec=JobEngine)
        engine.db = db
        engine.config = MagicMock()
        engine.config.slack_webhook_url = WEBHOOK
        engine.config.memory_enabled = False
        engine._artifact_uploader = None
        engine.archiver = None

        sent = AsyncMock(return_value=True)
        with patch("minions.notify.notify", new=sent):
            await JobEngine._on_job_terminal(engine, job.id)
            await JobEngine._on_job_terminal(engine, job.id)

        assert sent.await_count == 1
        events = [e for e in await db.get_events(job.id) if e.get("event_type") == "notify_terminal"]
        assert len(events) == 1
