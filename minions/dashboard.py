"""Web dashboard for Minions Suite. Read-only view of job state."""

import asyncio
import html
import json
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import aiosqlite
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from .config import Config

logger = logging.getLogger(__name__)

app = FastAPI(title="Minions Suite Dashboard")

_db_path: str = ""
_db_backend: str = "sqlite"
_postgres_url: str = ""


def _stringify_row(row: dict) -> dict:
    """Convert datetime values to ISO strings so template slicing ([:19]) works."""
    out = {}
    for k, v in row.items():
        if isinstance(v, datetime):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out


class _PgCursorWrapper:
    """Wraps a psycopg AsyncCursor to match the aiosqlite Row interface."""

    def __init__(self, cursor):
        self._cursor = cursor

    async def fetchone(self):
        row = await self._cursor.fetchone()
        if row is None:
            return None
        return _stringify_row(dict(row))

    async def fetchall(self):
        rows = await self._cursor.fetchall()
        return [_stringify_row(dict(r)) for r in rows]


class _PgConnectionWrapper:
    """Wraps a psycopg AsyncConnection to present db.execute() as an async context manager."""

    def __init__(self, conn):
        self._conn = conn

    _MINION_TABLES = ("jobs", "tasks", "agents", "events", "tool_calls", "messages", "subtasks", "state_transitions", "heartbeats", "message_log", "reviews")

    @asynccontextmanager
    async def execute(self, sql, params=None):
        # Translate SQLite ? placeholders to psycopg %s
        pg_sql = sql.replace("?", "%s")
        # Qualify table names with minions schema (PgBouncer resets search_path)
        for tbl in self._MINION_TABLES:
            pg_sql = pg_sql.replace(f"FROM {tbl}", f"FROM minions.{tbl}")
            pg_sql = pg_sql.replace(f"JOIN {tbl}", f"JOIN minions.{tbl}")
        cursor = self._conn.cursor()
        await cursor.execute(pg_sql, params)
        yield _PgCursorWrapper(cursor)

    async def close(self):
        await self._conn.close()


# Status -> CSS hex color (matches cli.py STATUS_COLORS)
STATUS_CSS = {
    "spec_received": "#00bcd4",
    "spec_ready": "#00bcd4",
    "tasks_created": "#2196f3",
    "dev_in_progress": "#ffc107",
    "pr_open": "#e040fb",
    "review_in_progress": "#ffc107",
    "merged": "#2196f3",
    "deploying": "#ffc107",
    "deployed": "#4caf50",
    "done": "#4caf50",
    "failed": "#f44336",
    "pending": "#888",
    "in_progress": "#ffc107",
    "in_review": "#e040fb",
    "starting": "#00bcd4",
    "running": "#ffc107",
    "completed": "#4caf50",
}


def _badge(status: str) -> str:
    color = STATUS_CSS.get(status, "#888")
    return f'<span style="background:{color}22;color:{color};border:1px solid {color}44;padding:2px 10px;border-radius:12px;font-size:0.85em;font-weight:600">{html.escape(status)}</span>'


def _elapsed(start_str, end_str=None) -> str:
    try:
        start = datetime.fromisoformat(start_str)
        end = datetime.fromisoformat(end_str) if end_str else datetime.now(UTC)
        secs = int((end - start).total_seconds())
        if secs < 60:
            return f"{secs}s"
        elif secs < 3600:
            return f"{secs // 60}m {secs % 60}s"
        return f"{secs // 3600}h {(secs % 3600) // 60}m"
    except (ValueError, TypeError):
        return "?"


def _esc(s) -> str:
    if s is None:
        return ""
    return html.escape(str(s))


async def _get_db():
    if _db_backend in ("postgres", "dual") and _postgres_url:
        import psycopg
        from psycopg.rows import dict_row

        conn = await psycopg.AsyncConnection.connect(
            _postgres_url, row_factory=dict_row, autocommit=True,
        )
        return _PgConnectionWrapper(conn)
    db = await aiosqlite.connect(_db_path)
    db.row_factory = aiosqlite.Row
    return db


# -- CSS --

CSS = """
* { margin:0; padding:0; box-sizing:border-box; }
body { background:#0d1117; color:#c9d1d9; font-family:'Segoe UI',system-ui,-apple-system,sans-serif; padding:20px; }
a { color:#58a6ff; text-decoration:none; }
a:hover { text-decoration:underline; }
h1 { color:#f0f6fc; margin-bottom:16px; font-size:1.5em; }
h2 { color:#f0f6fc; margin:24px 0 12px; font-size:1.2em; }
.cards { display:flex; gap:12px; margin-bottom:24px; flex-wrap:wrap; }
.card { background:#161b22; border:1px solid #30363d; border-radius:8px; padding:16px; min-width:120px; }
.card .num { font-size:2em; font-weight:700; color:#f0f6fc; }
.card .label { font-size:0.85em; color:#8b949e; }
.job-list { display:flex; flex-direction:column; gap:10px; }
.job-card { background:#161b22; border:1px solid #30363d; border-radius:8px; padding:16px; transition:border-color 0.2s; }
.job-card:hover { border-color:#58a6ff; }
.job-header { display:flex; justify-content:space-between; align-items:center; margin-bottom:8px; flex-wrap:wrap; gap:8px; }
.job-id { font-family:monospace; font-weight:700; color:#f0f6fc; }
.job-spec { color:#8b949e; font-size:0.9em; margin-bottom:8px; white-space:pre-wrap; word-break:break-word; }
.job-meta { display:flex; gap:16px; font-size:0.85em; color:#8b949e; flex-wrap:wrap; }
.task-counts span { margin-right:8px; }
table { width:100%; border-collapse:collapse; margin:12px 0; }
th { text-align:left; color:#8b949e; font-size:0.85em; padding:8px; border-bottom:1px solid #30363d; }
td { padding:8px; border-bottom:1px solid #21262d; font-size:0.9em; vertical-align:top; }
.timeline-row { font-family:monospace; font-size:0.85em; }
.timeline-ts { color:#8b949e; white-space:nowrap; }
.timeline-type { min-width:200px; }
.timeline-detail { color:#8b949e; word-break:break-all; max-width:600px; }
pre.spec { background:#0d1117; border:1px solid #30363d; border-radius:6px; padding:12px; white-space:pre-wrap; word-break:break-word; font-size:0.9em; color:#c9d1d9; max-height:400px; overflow-y:auto; }
.back { margin-bottom:16px; display:inline-block; }
.msg { background:#161b22; border:1px solid #30363d; border-radius:6px; padding:10px; margin:6px 0; }
.msg-header { font-size:0.8em; color:#8b949e; margin-bottom:4px; }
.msg-content { white-space:pre-wrap; word-break:break-word; }
"""


# -- Page shell --


def _page(title: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8"><title>{_esc(title)}</title>
<style>{CSS}</style>
<script src="https://unpkg.com/htmx.org@2.0.4"></script>
<script src="https://unpkg.com/idiomorph@0.3.0/dist/idiomorph-ext.min.js"></script>
</head><body hx-ext="morph">
<h1>Minions Suite Dashboard</h1>
{body}
</body></html>"""


# -- Routes --


@app.get("/", response_class=HTMLResponse)
async def index():
    db = await _get_db()
    try:
        # Summary counts
        async with db.execute("SELECT COUNT(*) as c FROM jobs") as cur:
            total = (await cur.fetchone())["c"]
        async with db.execute("SELECT COUNT(*) as c FROM jobs WHERE status NOT IN ('done','failed')") as cur:
            active = (await cur.fetchone())["c"]
        async with db.execute("SELECT COUNT(*) as c FROM jobs WHERE status = 'done'") as cur:
            done = (await cur.fetchone())["c"]
        async with db.execute("SELECT COUNT(*) as c FROM jobs WHERE status = 'failed'") as cur:
            failed = (await cur.fetchone())["c"]
        async with db.execute("SELECT COALESCE(SUM(cost_usd), 0.0) as c FROM agents") as cur:
            total_cost = (await cur.fetchone())["c"]
        async with db.execute("SELECT COALESCE(SUM(input_tokens + output_tokens), 0) as c FROM agents") as cur:
            total_tokens = (await cur.fetchone())["c"]

        cards = _render_summary_cards(total, active, done, failed, total_cost, total_tokens)

        # Job list (rendered as a swappable div for HTMX)
        job_html = await _render_job_list(db)

        body = f"""
        <div id="summary-cards" hx-get="/api/summary-html" hx-trigger="every 5s" hx-swap="morph:innerHTML">
        {cards}
        </div>
        <div id="job-list" hx-get="/api/jobs-html" hx-trigger="every 5s" hx-swap="morph:innerHTML">
        {job_html}
        </div>"""

        return _page("Dashboard", body)
    finally:
        await db.close()


def _render_summary_cards(total: int, active: int, done: int, failed: int, total_cost: float = 0.0, total_tokens: int = 0) -> str:
    cost_str = f"${total_cost:.2f}" if total_cost > 0 else "$0"
    tokens_str = f"{total_tokens:,}" if total_tokens > 0 else "0"
    return f"""<div class="cards">
        <div class="card"><div class="num">{total}</div><div class="label">Total</div></div>
        <div class="card"><div class="num" style="color:#ffc107">{active}</div><div class="label">Active</div></div>
        <div class="card"><div class="num" style="color:#4caf50">{done}</div><div class="label">Done</div></div>
        <div class="card"><div class="num" style="color:#f44336">{failed}</div><div class="label">Failed</div></div>
        <div class="card"><div class="num" style="color:#58a6ff">{cost_str}</div><div class="label">Total Cost</div></div>
        <div class="card"><div class="num" style="color:#58a6ff;font-size:1.2em">{tokens_str}</div><div class="label">Total Tokens</div></div>
    </div>"""


async def _render_job_list(db) -> str:
    async with db.execute("SELECT * FROM jobs ORDER BY created_at DESC") as cur:
        jobs = [dict(r) for r in await cur.fetchall()]

    if not jobs:
        return '<p style="color:#8b949e">No jobs yet.</p>'

    parts = []
    for job in jobs:
        job_id = job["id"]
        age = _elapsed(job["created_at"])
        spec_preview = _esc(job["spec"][:120]) + ("..." if len(job["spec"]) > 120 else "")

        # Task status counts
        async with db.execute("SELECT status, COUNT(*) as c FROM tasks WHERE job_id = ? GROUP BY status", (job_id,)) as cur:
            task_counts = {r["status"]: r["c"] for r in await cur.fetchall()}

        tc_html = ""
        if task_counts:
            tc_parts = []
            for st, count in task_counts.items():
                color = STATUS_CSS.get(st, "#888")
                tc_parts.append(f'<span style="color:{color}">{count} {st}</span>')
            tc_html = f'<div class="task-counts">{" ".join(tc_parts)}</div>'

        error_html = ""
        if job.get("error"):
            error_html = f'<div style="color:#f44336;font-size:0.85em;margin-top:4px">{_esc(job["error"][:200])}</div>'

        parts.append(
            f"""<a href="/job/{_esc(job_id)}" style="text-decoration:none;color:inherit">
        <div class="job-card">
            <div class="job-header">
                <span class="job-id">{_esc(job_id)}</span>
                {_badge(job["status"])}
            </div>
            <div class="job-spec">{spec_preview}</div>
            <div class="job-meta">
                <span>{age}</span>
                {tc_html}
            </div>
            {error_html}
        </div></a>"""
        )

    return '<div class="job-list">' + "\n".join(parts) + "</div>"


@app.get("/api/summary-html", response_class=HTMLResponse)
async def api_summary_html():
    """HTMX endpoint: returns summary cards HTML for swap."""
    db = await _get_db()
    try:
        async with db.execute("SELECT COUNT(*) as c FROM jobs") as cur:
            total = (await cur.fetchone())["c"]
        async with db.execute("SELECT COUNT(*) as c FROM jobs WHERE status NOT IN ('done','failed')") as cur:
            active = (await cur.fetchone())["c"]
        async with db.execute("SELECT COUNT(*) as c FROM jobs WHERE status = 'done'") as cur:
            done = (await cur.fetchone())["c"]
        async with db.execute("SELECT COUNT(*) as c FROM jobs WHERE status = 'failed'") as cur:
            failed = (await cur.fetchone())["c"]
        async with db.execute("SELECT COALESCE(SUM(cost_usd), 0.0) as c FROM agents") as cur:
            total_cost = (await cur.fetchone())["c"]
        async with db.execute("SELECT COALESCE(SUM(input_tokens + output_tokens), 0) as c FROM agents") as cur:
            total_tokens = (await cur.fetchone())["c"]
        return _render_summary_cards(total, active, done, failed, total_cost, total_tokens)
    finally:
        await db.close()


@app.get("/api/jobs-html", response_class=HTMLResponse)
async def api_jobs_html():
    """HTMX endpoint: returns just the job list HTML for swap."""
    db = await _get_db()
    try:
        return await _render_job_list(db)
    finally:
        await db.close()


@app.get("/api/jobs")
async def api_jobs():
    db = await _get_db()
    try:
        async with db.execute("SELECT * FROM jobs ORDER BY created_at DESC") as cur:
            jobs = [dict(r) for r in await cur.fetchall()]
        for job in jobs:
            job_id = job["id"]
            async with db.execute("SELECT status, COUNT(*) as c FROM tasks WHERE job_id = ? GROUP BY status", (job_id,)) as cur:
                job["task_counts"] = {r["status"]: r["c"] for r in await cur.fetchall()}
        return JSONResponse(jobs)
    finally:
        await db.close()


@app.get("/job/{job_id}", response_class=HTMLResponse)
async def job_detail(job_id: str):
    db = await _get_db()
    try:
        async with db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)) as cur:
            row = await cur.fetchone()
        if not row:
            return HTMLResponse(_page("Not Found", "<p>Job not found.</p>"), status_code=404)
        job = dict(row)

        age = _elapsed(job["created_at"])

        # Spec
        spec_html = f'<pre class="spec">{_esc(job["spec"])}</pre>'

        # Tasks
        async with db.execute("SELECT * FROM tasks WHERE job_id = ? ORDER BY created_at", (job_id,)) as cur:
            tasks = [dict(r) for r in await cur.fetchall()]

        task_rows = ""
        for t in tasks:
            pr_link = (
                f'<a href="{_esc(t["pr_url"])}" target="_blank">#{t["pr_number"]}</a>'
                if t.get("pr_url")
                else (str(t["pr_number"]) if t.get("pr_number") else "-")
            )
            err = f'<span style="color:#f44336">{_esc(str(t["error"])[:100])}</span>' if t.get("error") else "-"

            # Query subtasks for this task
            async with db.execute("SELECT * FROM subtasks WHERE task_id = ? ORDER BY sequence_num", (t["id"],)) as cur:
                subtasks = [dict(r) for r in await cur.fetchall()]

            subtask_info = ""
            if subtasks:
                completed = sum(1 for s in subtasks if s["status"] == "completed")
                subtask_info = f' <span style="color:#8b949e;font-size:0.85em">({completed}/{len(subtasks)} subtasks)</span>'

            task_rows += f"""<tr>
                <td>{_esc(t["title"])}{subtask_info}</td>
                <td>{_esc(t["service"])}</td>
                <td>{_esc(t["agent_role"])}</td>
                <td>{_badge(t["status"])}</td>
                <td>{pr_link}</td>
                <td>{err}</td>
            </tr>"""

            # Render subtask rows nested under the task
            for s in subtasks:
                s_err = f'<span style="color:#f44336">{_esc(str(s["error"])[:80])}</span>' if s.get("error") else ""
                task_rows += f"""<tr style="background:#0d1117">
                    <td style="padding-left:32px;color:#8b949e;font-size:0.85em" colspan="3">{s["sequence_num"]}. {_esc(s["description"][:100])}</td>
                    <td>{_badge(s["status"])}</td>
                    <td colspan="2">{s_err}</td>
                </tr>"""

        tasks_html = (
            f"""<table>
            <tr><th>Title</th><th>Service</th><th>Agent Role</th><th>Status</th><th>PR</th><th>Error</th></tr>
            {task_rows}
        </table>"""
            if tasks
            else '<p style="color:#8b949e">No tasks.</p>'
        )

        # Agents
        async with db.execute("SELECT * FROM agents WHERE job_id = ? ORDER BY started_at", (job_id,)) as cur:
            agents = [dict(r) for r in await cur.fetchall()]

        agent_rows = ""
        for a in agents:
            dur = _elapsed(a["started_at"], a["finished_at"]) if a.get("finished_at") else "running"
            log_link = f'<span style="font-family:monospace;font-size:0.85em">{_esc(a["log_file"])}</span>' if a.get("log_file") else "-"
            err = f'<span style="color:#f44336">{_esc(str(a["error"])[:100])}</span>' if a.get("error") else "-"
            tokens = f'{a.get("input_tokens", 0):,} / {a.get("output_tokens", 0):,}'
            cost = f'${a.get("cost_usd", 0):.4f}' if a.get("cost_usd") else "-"
            turns = str(a.get("num_turns", 0)) if a.get("num_turns") else "-"
            model = _esc(a.get("model") or "-")
            agent_rows += f"""<tr>
                <td>{_esc(a["role"])}</td>
                <td>{_badge(a["status"])}</td>
                <td>{dur}</td>
                <td>{tokens}</td>
                <td>{cost}</td>
                <td>{turns}</td>
                <td>{model}</td>
                <td>{log_link}</td>
                <td>{err}</td>
            </tr>"""

        agents_html = (
            f"""<table>
            <tr><th>Role</th><th>Status</th><th>Duration</th><th>Tokens (in/out)</th><th>Cost</th><th>Turns</th><th>Model</th><th>Log</th><th>Error</th></tr>
            {agent_rows}
        </table>"""
            if agents
            else '<p style="color:#8b949e">No agents.</p>'
        )

        # Timeline (events + tool_calls merged)
        events = []
        async with db.execute("SELECT * FROM events WHERE job_id = ? ORDER BY created_at", (job_id,)) as cur:
            for r in await cur.fetchall():
                e = dict(r)
                events.append({"ts": e["created_at"], "type": "event", "name": e["event_type"], "detail": e.get("detail", "")})
        async with db.execute("SELECT * FROM tool_calls WHERE job_id = ? ORDER BY created_at", (job_id,)) as cur:
            for r in await cur.fetchall():
                tc = dict(r)
                detail_parts = []
                if tc.get("params"):
                    try:
                        p = json.loads(tc["params"]) if isinstance(tc["params"], str) else tc["params"]
                        ps = json.dumps(p, indent=None, default=str)
                        if len(ps) > 120:
                            ps = ps[:120] + "..."
                        detail_parts.append(ps)
                    except (json.JSONDecodeError, TypeError):
                        pass
                if tc.get("duration_ms") is not None:
                    detail_parts.append(f"{tc['duration_ms']:.0f}ms")
                if tc.get("error"):
                    detail_parts.append(f"ERR: {tc['error'][:80]}")
                events.append({"ts": tc["created_at"], "type": "tool_call", "name": f"tool:{tc['tool_name']}", "detail": " | ".join(detail_parts)})

        events.sort(key=lambda x: x["ts"], reverse=True)

        type_colors = {"event": "#ffc107", "tool_call": "#00bcd4"}
        timeline_rows = ""
        for e in events:
            color = type_colors.get(e["type"], "#888")
            timeline_rows += f"""<tr class="timeline-row">
                <td class="timeline-ts">{_esc(e["ts"][:19])}</td>
                <td class="timeline-type" style="color:{color}">{_esc(e["name"])}</td>
                <td class="timeline-detail">{_esc(e["detail"])}</td>
            </tr>"""

        timeline_html = f"""<table>{timeline_rows}</table>""" if events else '<p style="color:#8b949e">No events.</p>'

        # Messages
        async with db.execute("SELECT * FROM messages WHERE job_id = ? ORDER BY created_at DESC", (job_id,)) as cur:
            messages = [dict(r) for r in await cur.fetchall()]

        msgs_html = ""
        if messages:
            for m in messages:
                to_str = f" -> {_esc(m['to_role'])}" if m.get("to_role") else " (broadcast)"
                msgs_html += f"""<div class="msg">
                    <div class="msg-header">{_esc(m["from_role"])}{to_str} at {_esc(m["created_at"][:19])}</div>
                    <div class="msg-content">{_esc(m["content"][:500])}</div>
                </div>"""
        else:
            msgs_html = '<p style="color:#8b949e">No messages.</p>'

        # Detail body with HTMX auto-refresh on the detail section
        detail_content = f"""
            <h2>Spec</h2>
            {spec_html}
            <h2>Tasks</h2>
            {tasks_html}
            <h2>Agents</h2>
            {agents_html}
            <h2>Timeline ({len(events)} entries)</h2>
            {timeline_html}
            <h2>Messages</h2>
            {msgs_html}
        """

        body = f"""
        <a href="/" class="back">&larr; All Jobs</a>
        <div class="job-header" style="margin-bottom:16px">
            <span class="job-id" style="font-size:1.2em">{_esc(job_id)}</span>
            {_badge(job["status"])}
            <span style="color:#8b949e">{age}</span>
        </div>
        <div id="job-detail" hx-get="/api/job/{_esc(job_id)}/html" hx-trigger="every 5s" hx-swap="morph:innerHTML">
        {detail_content}
        </div>
        """

        return HTMLResponse(_page(f"Job {job_id}", body))
    finally:
        await db.close()


@app.get("/api/job/{job_id}/html", response_class=HTMLResponse)
async def api_job_detail_html(job_id: str):
    """HTMX endpoint: returns the refreshable detail section."""
    db = await _get_db()
    try:
        async with db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)) as cur:
            row = await cur.fetchone()
        if not row:
            return HTMLResponse('<p style="color:#f44336">Job not found.</p>')
        job = dict(row)

        parts = []

        # Spec
        parts.append(f'<h2>Spec</h2><pre class="spec">{_esc(job["spec"])}</pre>')

        # Tasks
        async with db.execute("SELECT * FROM tasks WHERE job_id = ? ORDER BY created_at", (job_id,)) as cur:
            tasks = [dict(r) for r in await cur.fetchall()]
        if tasks:
            rows = ""
            for t in tasks:
                pr_link = (
                    f'<a href="{_esc(t["pr_url"])}" target="_blank">#{t["pr_number"]}</a>'
                    if t.get("pr_url")
                    else (str(t["pr_number"]) if t.get("pr_number") else "-")
                )
                err = f'<span style="color:#f44336">{_esc(str(t["error"])[:100])}</span>' if t.get("error") else "-"

                # Query subtasks for this task
                async with db.execute("SELECT * FROM subtasks WHERE task_id = ? ORDER BY sequence_num", (t["id"],)) as cur:
                    subtasks = [dict(r) for r in await cur.fetchall()]

                subtask_info = ""
                if subtasks:
                    completed = sum(1 for s in subtasks if s["status"] == "completed")
                    subtask_info = f' <span style="color:#8b949e;font-size:0.85em">({completed}/{len(subtasks)} subtasks)</span>'

                rows += f'<tr><td>{_esc(t["title"])}{subtask_info}</td><td>{_esc(t["service"])}</td><td>{_esc(t["agent_role"])}</td><td>{_badge(t["status"])}</td><td>{pr_link}</td><td>{err}</td></tr>'

                for s in subtasks:
                    s_err = f'<span style="color:#f44336">{_esc(str(s["error"])[:80])}</span>' if s.get("error") else ""
                    rows += f'<tr style="background:#0d1117"><td style="padding-left:32px;color:#8b949e;font-size:0.85em" colspan="3">{s["sequence_num"]}. {_esc(s["description"][:100])}</td><td>{_badge(s["status"])}</td><td colspan="2">{s_err}</td></tr>'

            parts.append(
                f"<h2>Tasks</h2><table><tr><th>Title</th><th>Service</th><th>Agent Role</th><th>Status</th><th>PR</th><th>Error</th></tr>{rows}</table>"
            )
        else:
            parts.append('<h2>Tasks</h2><p style="color:#8b949e">No tasks.</p>')

        # Agents
        async with db.execute("SELECT * FROM agents WHERE job_id = ? ORDER BY started_at", (job_id,)) as cur:
            agents = [dict(r) for r in await cur.fetchall()]
        if agents:
            rows = ""
            for a in agents:
                dur = _elapsed(a["started_at"], a["finished_at"]) if a.get("finished_at") else "running"
                log_link = f'<span style="font-family:monospace;font-size:0.85em">{_esc(a["log_file"])}</span>' if a.get("log_file") else "-"
                err = f'<span style="color:#f44336">{_esc(str(a["error"])[:100])}</span>' if a.get("error") else "-"
                tokens = f'{a.get("input_tokens", 0):,} / {a.get("output_tokens", 0):,}'
                cost = f'${a.get("cost_usd", 0):.4f}' if a.get("cost_usd") else "-"
                turns = str(a.get("num_turns", 0)) if a.get("num_turns") else "-"
                model = _esc(a.get("model") or "-")
                rows += f'<tr><td>{_esc(a["role"])}</td><td>{_badge(a["status"])}</td><td>{dur}</td><td>{tokens}</td><td>{cost}</td><td>{turns}</td><td>{model}</td><td>{log_link}</td><td>{err}</td></tr>'
            parts.append(
                f"<h2>Agents</h2><table><tr><th>Role</th><th>Status</th><th>Duration</th><th>Tokens (in/out)</th><th>Cost</th><th>Turns</th><th>Model</th><th>Log</th><th>Error</th></tr>{rows}</table>"
            )
        else:
            parts.append('<h2>Agents</h2><p style="color:#8b949e">No agents.</p>')

        # Timeline (events + tool_calls merged)
        events = []
        async with db.execute("SELECT * FROM events WHERE job_id = ? ORDER BY created_at", (job_id,)) as cur:
            for r in await cur.fetchall():
                e = dict(r)
                events.append({"ts": e["created_at"], "type": "event", "name": e["event_type"], "detail": e.get("detail", "")})
        async with db.execute("SELECT * FROM tool_calls WHERE job_id = ? ORDER BY created_at", (job_id,)) as cur:
            for r in await cur.fetchall():
                tc = dict(r)
                detail_parts = []
                if tc.get("params"):
                    try:
                        p = json.loads(tc["params"]) if isinstance(tc["params"], str) else tc["params"]
                        ps = json.dumps(p, indent=None, default=str)
                        if len(ps) > 120:
                            ps = ps[:120] + "..."
                        detail_parts.append(ps)
                    except (json.JSONDecodeError, TypeError):
                        pass
                if tc.get("duration_ms") is not None:
                    detail_parts.append(f"{tc['duration_ms']:.0f}ms")
                if tc.get("error"):
                    detail_parts.append(f"ERR: {tc['error'][:80]}")
                events.append({"ts": tc["created_at"], "type": "tool_call", "name": f"tool:{tc['tool_name']}", "detail": " | ".join(detail_parts)})

        events.sort(key=lambda x: x["ts"], reverse=True)

        type_colors = {"event": "#ffc107", "tool_call": "#00bcd4"}
        if events:
            timeline_rows = ""
            for e in events:
                color = type_colors.get(e["type"], "#888")
                timeline_rows += f"""<tr class="timeline-row">
                    <td class="timeline-ts">{_esc(e["ts"][:19])}</td>
                    <td class="timeline-type" style="color:{color}">{_esc(e["name"])}</td>
                    <td class="timeline-detail">{_esc(e["detail"])}</td>
                </tr>"""
            parts.append(f"<h2>Timeline ({len(events)} entries)</h2><table>{timeline_rows}</table>")
        else:
            parts.append('<h2>Timeline</h2><p style="color:#8b949e">No events.</p>')

        # Messages
        async with db.execute("SELECT * FROM messages WHERE job_id = ? ORDER BY created_at DESC", (job_id,)) as cur:
            messages = [dict(r) for r in await cur.fetchall()]

        if messages:
            msgs_html = ""
            for m in messages:
                to_str = f" -> {_esc(m['to_role'])}" if m.get("to_role") else " (broadcast)"
                msgs_html += f"""<div class="msg">
                    <div class="msg-header">{_esc(m["from_role"])}{to_str} at {_esc(m["created_at"][:19])}</div>
                    <div class="msg-content">{_esc(m["content"][:500])}</div>
                </div>"""
            parts.append(f"<h2>Messages ({len(messages)})</h2>{msgs_html}")
        else:
            parts.append('<h2>Messages</h2><p style="color:#8b949e">No messages.</p>')

        return "\n".join(parts)
    finally:
        await db.close()


@app.get("/api/job/{job_id}")
async def api_job_detail(job_id: str):
    db = await _get_db()
    try:
        async with db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)) as cur:
            row = await cur.fetchone()
        if not row:
            return JSONResponse({"error": "not found"}, status_code=404)
        job = dict(row)

        async with db.execute("SELECT * FROM tasks WHERE job_id = ? ORDER BY created_at", (job_id,)) as cur:
            job["tasks"] = [dict(r) for r in await cur.fetchall()]
        async with db.execute("SELECT * FROM agents WHERE job_id = ? ORDER BY started_at", (job_id,)) as cur:
            job["agents"] = [dict(r) for r in await cur.fetchall()]
        async with db.execute("SELECT * FROM messages WHERE job_id = ? ORDER BY created_at DESC", (job_id,)) as cur:
            job["messages"] = [dict(r) for r in await cur.fetchall()]

        return JSONResponse(job)
    finally:
        await db.close()


@app.get("/api/events/stream")
async def events_stream():
    """SSE endpoint streaming real-time NATS system events.

    Only active when NATS_ENABLED=true. Falls back to a no-op stream otherwise.
    """
    config = Config.from_env()
    if not config.nats_enabled:

        async def _empty():
            yield 'data: {"info": "NATS not enabled, SSE inactive"}\n\n'

        return StreamingResponse(_empty(), media_type="text/event-stream")

    from .connectors.nats_subscriber import subscribe_events

    async def _generate():
        try:
            async for event in subscribe_events("system.events"):
                yield f"data: {json.dumps(event)}\n\n"
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.warning(f"SSE stream error: {e}")
            yield f'data: {{"error": "{str(e)[:200]}"}}\n\n'

    return StreamingResponse(_generate(), media_type="text/event-stream")


def run_dashboard(port: int = 8322):
    """Start the dashboard web server."""
    import uvicorn

    global _db_path, _db_backend, _postgres_url
    config = Config.from_env()
    _db_path = config.db_path
    _db_backend = config.db_backend
    _postgres_url = config.postgres_url

    if _db_backend in ("postgres", "dual") and _postgres_url:
        logger.info("Starting dashboard on http://localhost:%d", port)
        logger.info("Reading from database: postgres (search_path=minions)")
    else:
        logger.info("Starting dashboard on http://localhost:%d", port)
        logger.info("Reading from database: %s", _db_path)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
