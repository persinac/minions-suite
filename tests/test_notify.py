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
