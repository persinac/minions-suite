"""Web dashboard for Minions Suite. Read-only view of job state."""

import asyncio
import html
import json
import logging
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from .config import Config

logger = logging.getLogger(__name__)

app = FastAPI(title="Minions Suite Dashboard")

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
    """Wraps a psycopg AsyncCursor to yield plain JSON-ready dicts.

    The shape (and the `?` placeholders in the queries below) is inherited from
    the removed SQLite backend. Postgres is the only backend now, so this is
    dialect translation for no one — but the queries were written against it,
    so collapsing it means rewriting all 39 placeholder sites at once. Left
    deliberately; see the note on _PgConnectionWrapper.execute.
    """

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

    _MINION_TABLES = (
        "jobs",
        "tasks",
        "agents",
        "events",
        "tool_calls",
        "messages",
        "subtasks",
        "state_transitions",
        "heartbeats",
        "message_log",
        "reviews",
        "memory_nodes",
        "memory_links",
        "memory_entities",
        "memory_operations",
    )

    @asynccontextmanager
    async def execute(self, sql, params=None):
        # Translate ? placeholders to psycopg %s. Vestigial — the SQLite backend
        # these were written for is gone. Kept because the alternative is editing
        # every query in this file in one pass, against thin test coverage.
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
    "no_work_needed": "#26a69a",
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
    import psycopg
    from psycopg.rows import dict_row

    conn = await psycopg.AsyncConnection.connect(
        _postgres_url,
        row_factory=dict_row,
        autocommit=True,
    )
    return _PgConnectionWrapper(conn)


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
.nav { margin-bottom:20px; display:flex; gap:16px; align-items:center; border-bottom:1px solid #30363d; padding-bottom:12px; }
.nav a { font-weight:600; padding:4px 12px; border-radius:6px; }
.nav a.active { background:#58a6ff22; color:#58a6ff; }
.back { margin-bottom:16px; display:inline-block; }

/* Memory page */
.memory-grid { display:grid; grid-template-columns:1fr 1fr; gap:16px; margin-bottom:24px; }
@media (max-width:900px) { .memory-grid { grid-template-columns:1fr; } }
.tier-card { background:#161b22; border:1px solid #30363d; border-radius:8px; padding:16px; }
.tier-card h3 { color:#f0f6fc; margin-bottom:8px; font-size:1em; }
.tier-card .count { font-size:2em; font-weight:700; }
.tier-l2 .count { color:#00bcd4; }
.tier-l3 .count { color:#2196f3; }
.tier-entity .count { color:#e040fb; }
.tier-link .count { color:#ffc107; }
.tier-ops .count { color:#4caf50; }
.graph-container { background:#161b22; border:1px solid #30363d; border-radius:8px; padding:16px; margin-bottom:24px; }
.graph-container h2 { margin-bottom:12px; }
#memory-graph { width:100%; height:500px; }
.ops-timeline { background:#161b22; border:1px solid #30363d; border-radius:8px; padding:16px; }
.ops-timeline h2 { margin-bottom:12px; }
.op-row { display:flex; gap:12px; padding:6px 0; border-bottom:1px solid #21262d; font-size:0.9em; align-items:center; }
.op-ts { color:#8b949e; font-family:monospace; white-space:nowrap; min-width:80px; }
.op-tier { padding:2px 8px; border-radius:4px; font-weight:600; font-size:0.85em; min-width:80px; text-align:center; }
.op-tier.l2 { background:#00bcd422; color:#00bcd4; }
.op-tier.l3 { background:#2196f322; color:#2196f3; }
.op-tier.retrieval { background:#ffc10722; color:#ffc107; }
.op-tier.context { background:#4caf5022; color:#4caf50; }
.op-tier.archive { background:#e040fb22; color:#e040fb; }
.op-tier.unknown { background:#88888822; color:#888; }
.op-name { color:#c9d1d9; font-weight:600; min-width:160px; }
.op-detail { color:#8b949e; flex:1; word-break:break-word; }
.op-dur { color:#8b949e; font-family:monospace; min-width:60px; text-align:right; }
.project-select { background:#0d1117; color:#c9d1d9; border:1px solid #30363d; border-radius:6px; padding:6px 12px; font-size:0.9em; }
.msg { background:#161b22; border:1px solid #30363d; border-radius:6px; padding:10px; margin:6px 0; }
.msg-header { font-size:0.8em; color:#8b949e; margin-bottom:4px; }
.msg-content { white-space:pre-wrap; word-break:break-word; }

/* Review Rounds */
.review-rounds { margin:16px 0; }
.round-service { background:#161b22; border:1px solid #30363d; border-radius:8px; padding:16px; margin:12px 0; }
.round-service h3 { color:#f0f6fc; font-size:1em; margin-bottom:12px; font-weight:700; }
.rounds-summary { color:#8b949e; font-size:0.85em; margin-bottom:16px; padding:8px 12px; background:#0d1117; border-radius:6px; display:flex; gap:16px; flex-wrap:wrap; }
.rounds-summary .stat { font-weight:600; }
.rounds-summary .stat-val { color:#f0f6fc; }
.round-card { border:1px solid #30363d; border-radius:6px; padding:12px; margin:8px 0; background:#0d1117; }
.round-header { display:flex; justify-content:space-between; align-items:center; margin-bottom:8px; }
.round-num { font-weight:700; color:#f0f6fc; font-size:0.95em; }
.verdict-badge { padding:2px 10px; border-radius:12px; font-size:0.85em; font-weight:600; }
.verdict-approve { background:#4caf5022; color:#4caf50; border:1px solid #4caf5044; }
.verdict-changes { background:#f4433622; color:#f44336; border:1px solid #f4433644; }
.verdict-unknown { background:#88888822; color:#888; border:1px solid #88888844; }
.round-agents { display:flex; align-items:center; gap:8px; flex-wrap:wrap; margin-bottom:8px; }
.round-agent { display:flex; align-items:center; gap:6px; font-size:0.85em; padding:6px 10px; border-radius:4px; }
.round-agent.eng { background:#1a3a2a; border:1px solid #2d5a3d; }
.round-agent.rev { background:#2a1a3a; border:1px solid #4a2d5a; }
.round-connector { color:#30363d; font-size:1.2em; font-weight:700; }
.agent-role { font-weight:600; color:#c9d1d9; }
.agent-meta { color:#8b949e; font-size:0.85em; }
.round-threads { margin-top:8px; display:flex; gap:12px; font-size:0.85em; align-items:center; flex-wrap:wrap; }
.sev-count { padding:2px 8px; border-radius:3px; font-weight:600; font-size:0.85em; }
.sev-count.critical { background:#f4433622; color:#f44336; }
.sev-count.warning { background:#ffc10722; color:#ffc107; }
.sev-count.nit { background:#8b949e22; color:#8b949e; }
.thread-details { margin-top:8px; }
.thread-details summary { color:#58a6ff; cursor:pointer; font-size:0.85em; }
.thread-details summary:hover { text-decoration:underline; }
.thread-table { width:100%; font-size:0.85em; margin-top:6px; }
.thread-table td { padding:3px 8px; border-bottom:1px solid #21262d; vertical-align:top; }
.thread-table .file-ref { color:#58a6ff; font-family:monospace; white-space:nowrap; }
.thread-table .thread-body { color:#c9d1d9; word-break:break-word; max-width:500px; }
.round-arrow { text-align:center; color:#30363d; font-size:1.4em; line-height:1; margin:4px 0; }
.resolution-info { font-size:0.85em; margin-top:6px; display:flex; gap:12px; flex-wrap:wrap; }
.resolution-info .resolved { color:#4caf50; }
.resolution-info .persisted { color:#ffc107; }
.resolution-info .new-threads { color:#58a6ff; }
.round-feedback { margin-top:8px; padding:8px; background:#161b22; border-left:3px solid #30363d; font-size:0.85em; color:#8b949e; max-height:120px; overflow-y:auto; white-space:pre-wrap; word-break:break-word; }
"""


# -- Page shell --


def _page(title: str, body: str, active: str = "jobs") -> str:
    jobs_cls = ' class="active"' if active == "jobs" else ""
    mem_cls = ' class="active"' if active == "memory" else ""
    return f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8"><title>{_esc(title)}</title>
<style>{CSS}</style>
<script src="https://unpkg.com/htmx.org@2.0.4"></script>
<script src="https://unpkg.com/idiomorph@0.3.0/dist/idiomorph-ext.min.js"></script>
</head><body hx-ext="morph">
<h1>Minions Suite Dashboard</h1>
<nav class="nav"><a href="/"{jobs_cls}>Jobs</a><a href="/memory"{mem_cls}>Memory</a></nav>
{body}
</body></html>"""


# -- Review Rounds --


async def _build_review_rounds(db, job_id: str) -> list[dict]:
    """Build review round data grouped by service.

    Each round represents one engineer→reviewer cycle. Returns a list of
    per-service dicts containing rounds with agent info, verdict, threads,
    and resolution tracking.
    """
    engineer_roles = {"backend_engineer", "frontend_engineer", "database_engineer"}

    async with db.execute("SELECT * FROM tasks WHERE job_id = ? ORDER BY created_at", (job_id,)) as cur:
        tasks = [dict(r) for r in await cur.fetchall()]

    async with db.execute("SELECT * FROM agents WHERE job_id = ? ORDER BY started_at", (job_id,)) as cur:
        agents = [dict(r) for r in await cur.fetchall()]

    async with db.execute(
        "SELECT * FROM tool_calls WHERE job_id = ? AND tool_name IN ('post_inline_comment', 'submit_review', 'report_review_complete') ORDER BY created_at",
        (job_id,),
    ) as cur:
        review_tool_calls = [dict(r) for r in await cur.fetchall()]

    async with db.execute(
        "SELECT * FROM messages WHERE job_id = ? AND from_role = 'code_reviewer' ORDER BY created_at",
        (job_id,),
    ) as cur:
        review_messages = [dict(r) for r in await cur.fetchall()]

    # Group tasks by service
    eng_tasks_by_svc = {}
    rev_tasks_by_svc = {}
    for t in tasks:
        if t["service"] in ("_spec", "_arbiter"):
            continue
        if t["agent_role"] in engineer_roles:
            eng_tasks_by_svc.setdefault(t["service"], []).append(t)
        elif t["agent_role"] == "code_reviewer":
            rev_tasks_by_svc.setdefault(t["service"], []).append(t)

    # Agents lookup by task_id
    agents_by_task = {}
    for a in agents:
        if a.get("task_id"):
            agents_by_task.setdefault(a["task_id"], []).append(a)

    services = []
    for svc_name in eng_tasks_by_svc:
        eng_tasks = eng_tasks_by_svc[svc_name]
        rev_tasks = rev_tasks_by_svc.get(svc_name, [])

        if not rev_tasks:
            continue

        eng_task = eng_tasks[0]
        eng_agents = agents_by_task.get(eng_task["id"], [])

        rounds = []
        for i, rev_task in enumerate(rev_tasks):
            round_num = i + 1

            # Reviewer agent
            rev_agents = agents_by_task.get(rev_task["id"], [])
            rev_agent = rev_agents[-1] if rev_agents else None

            # Engineer agent preceding this review (by timestamp)
            eng_agent = None
            if rev_agent and eng_agents:
                rev_start = rev_agent.get("started_at", "")
                preceding = [a for a in eng_agents if a.get("started_at", "") < rev_start]
                if preceding:
                    eng_agent = preceding[-1]
                elif eng_agents:
                    eng_agent = eng_agents[min(i, len(eng_agents) - 1)]

            # Extract threads from post_inline_comment tool_calls within reviewer's window
            threads = []
            submit_verdict = None
            submit_summary = None
            if rev_agent:
                rev_start = rev_agent.get("started_at", "")
                rev_end = rev_agent.get("finished_at") or "9999"
                for tc in review_tool_calls:
                    tc_ts = tc["created_at"]
                    if not (rev_start <= tc_ts <= rev_end):
                        continue
                    params = {}
                    if tc.get("params"):
                        try:
                            params = json.loads(tc["params"]) if isinstance(tc["params"], str) else tc["params"]
                        except (json.JSONDecodeError, TypeError):
                            pass
                    if tc["tool_name"] == "post_inline_comment":
                        threads.append(
                            {
                                "file": params.get("file_path", "?"),
                                "line": params.get("line", 0),
                                "severity": params.get("severity", "nit"),
                                "body": params.get("body", "")[:200],
                            }
                        )
                    elif tc["tool_name"] == "submit_review":
                        submit_verdict = params.get("verdict")
                        submit_summary = params.get("body", "")[:500]

            # Thread resolution vs previous round
            resolved = 0
            persisted = 0
            new_count = 0
            if round_num > 1 and rounds:
                prev_threads = rounds[-1].get("threads", [])
                prev_files = {(t["file"], t["line"]) for t in prev_threads}
                curr_files = {(t["file"], t["line"]) for t in threads}
                resolved = len(prev_files - curr_files)
                persisted = len(prev_files & curr_files)
                new_count = len(curr_files - prev_files)

            # Feedback message from reviewer
            feedback = None
            for msg in review_messages:
                if rev_agent:
                    msg_ts = msg["created_at"]
                    if rev_agent.get("started_at", "") <= msg_ts <= (rev_agent.get("finished_at") or "9999"):
                        feedback = msg["content"][:500]
                        break

            verdict = rev_task.get("verdict") or submit_verdict or "unknown"
            comments = rev_task.get("comments_posted", 0) or len(threads)

            rounds.append(
                {
                    "num": round_num,
                    "eng_agent": eng_agent,
                    "rev_agent": rev_agent,
                    "rev_task": rev_task,
                    "verdict": verdict,
                    "comments": comments,
                    "threads": threads,
                    "resolved": resolved,
                    "persisted": persisted,
                    "new_count": new_count,
                    "feedback": feedback,
                    "submit_summary": submit_summary,
                }
            )

        total_threads = sum(r["comments"] for r in rounds)
        total_resolved = sum(r["resolved"] for r in rounds)

        services.append(
            {
                "service": svc_name,
                "eng_task": eng_task,
                "rounds": rounds,
                "total_threads": total_threads,
                "total_resolved": total_resolved,
            }
        )

    return services


def _render_review_rounds(services_data: list[dict]) -> str:
    """Render the review rounds HTML from pre-built data."""
    if not services_data:
        return ""

    parts = ['<h2>Review Rounds</h2><div class="review-rounds">']

    for svc in services_data:
        svc_name = _esc(svc["service"])
        rounds = svc["rounds"]
        total_threads = svc["total_threads"]
        total_resolved = svc["total_resolved"]
        eng_task = svc["eng_task"]
        revision_count = eng_task.get("revision_count", 0)

        parts.append(f'<div class="round-service"><h3>{svc_name}</h3>')
        parts.append('<div class="rounds-summary">')
        parts.append(f'<span class="stat"><span class="stat-val">{len(rounds)}</span> rounds</span>')
        parts.append(f'<span class="stat"><span class="stat-val">{total_threads}</span> threads opened</span>')
        if total_resolved > 0:
            parts.append(f'<span class="stat"><span class="stat-val">{total_resolved}</span> resolved</span>')
        if revision_count > 0:
            parts.append(f'<span class="stat"><span class="stat-val">{revision_count}</span> revisions</span>')
        parts.append("</div>")

        for rnd in rounds:
            verdict = rnd["verdict"]
            if verdict == "approve" or verdict == "approved":
                verdict_class = "verdict-approve"
                verdict_label = "approved"
            elif verdict == "request_changes" or verdict == "changes_requested":
                verdict_class = "verdict-changes"
                verdict_label = "changes requested"
            else:
                verdict_class = "verdict-unknown"
                verdict_label = _esc(verdict)

            parts.append('<div class="round-card">')
            parts.append(f'<div class="round-header"><span class="round-num">Round {rnd["num"]}</span>')
            parts.append(f'<span class="verdict-badge {verdict_class}">{verdict_label}</span></div>')

            # Agent row
            parts.append('<div class="round-agents">')
            eng = rnd["eng_agent"]
            if eng:
                eng_dur = _elapsed(eng.get("started_at", ""), eng.get("finished_at"))
                eng_cost = f"${eng.get('cost_usd', 0):.3f}"
                eng_turns = eng.get("num_turns", 0)
                parts.append(
                    f'<div class="round-agent eng">'
                    f'<span class="agent-role">{_esc(eng.get("role", "engineer"))}</span>'
                    f'<span class="agent-meta">{eng_dur} &middot; {eng_cost} &middot; {eng_turns} turns</span>'
                    f"</div>"
                )
            parts.append('<span class="round-connector">&rarr;</span>')
            rev = rnd["rev_agent"]
            if rev:
                rev_dur = _elapsed(rev.get("started_at", ""), rev.get("finished_at"))
                rev_cost = f"${rev.get('cost_usd', 0):.3f}"
                rev_turns = rev.get("num_turns", 0)
                parts.append(
                    f'<div class="round-agent rev">'
                    f'<span class="agent-role">code_reviewer</span>'
                    f'<span class="agent-meta">{rev_dur} &middot; {rev_cost} &middot; {rev_turns} turns</span>'
                    f"</div>"
                )
            elif rnd["rev_task"]:
                parts.append(
                    f'<div class="round-agent rev">'
                    f'<span class="agent-role">code_reviewer</span>'
                    f'<span class="agent-meta">{rnd["comments"]} comments</span>'
                    f"</div>"
                )
            parts.append("</div>")

            # Resolution info (round 2+)
            if rnd["num"] > 1 and (rnd["resolved"] or rnd["persisted"] or rnd["new_count"]):
                parts.append('<div class="resolution-info">')
                if rnd["resolved"]:
                    parts.append(f'<span class="resolved">{rnd["resolved"]} resolved</span>')
                if rnd["persisted"]:
                    parts.append(f'<span class="persisted">{rnd["persisted"]} persisted</span>')
                if rnd["new_count"]:
                    parts.append(f'<span class="new-threads">{rnd["new_count"]} new</span>')
                parts.append("</div>")

            # Thread severity breakdown
            threads = rnd["threads"]
            if threads:
                sev_counts = {"critical": 0, "warning": 0, "nit": 0}
                for t in threads:
                    sev = t.get("severity", "nit")
                    if sev in sev_counts:
                        sev_counts[sev] += 1
                    else:
                        sev_counts["nit"] += 1

                parts.append('<div class="round-threads">')
                if sev_counts["critical"]:
                    parts.append(f'<span class="sev-count critical">{sev_counts["critical"]} critical</span>')
                if sev_counts["warning"]:
                    parts.append(f'<span class="sev-count warning">{sev_counts["warning"]} warning</span>')
                if sev_counts["nit"]:
                    parts.append(f'<span class="sev-count nit">{sev_counts["nit"]} nit</span>')
                parts.append("</div>")

                # Expandable thread list
                parts.append(f'<details class="thread-details"><summary>{len(threads)} threads</summary>')
                parts.append('<table class="thread-table">')
                for t in threads:
                    sev = t.get("severity", "nit")
                    sev_class = sev if sev in ("critical", "warning", "nit") else "nit"
                    parts.append(
                        f"<tr>"
                        f'<td><span class="sev-count {sev_class}">{_esc(sev).upper()}</span></td>'
                        f'<td class="file-ref">{_esc(t["file"])}:{t["line"]}</td>'
                        f'<td class="thread-body">{_esc(t["body"])}</td>'
                        f"</tr>"
                    )
                parts.append("</table></details>")
            elif rnd["comments"] > 0:
                parts.append(
                    f'<div class="round-threads"><span style="color:#8b949e">{rnd["comments"]} comments (detail available on next run)</span></div>'
                )

            # Feedback excerpt
            if rnd["submit_summary"]:
                parts.append(f'<div class="round-feedback">{_esc(rnd["submit_summary"][:300])}</div>')
            elif rnd["feedback"]:
                parts.append(f'<div class="round-feedback">{_esc(rnd["feedback"][:300])}</div>')

            parts.append("</div>")  # close round-card

            # Arrow between rounds (not after last)
            if rnd["num"] < len(rounds):
                parts.append('<div class="round-arrow">&darr;</div>')

        parts.append("</div>")  # close round-service

    parts.append("</div>")  # close review-rounds
    return "\n".join(parts)


# -- Routes --


@app.get("/", response_class=HTMLResponse)
async def index():
    db = await _get_db()
    try:
        # Summary counts
        async with db.execute("SELECT COUNT(*) as c FROM jobs") as cur:
            total = (await cur.fetchone())["c"]
        async with db.execute("SELECT COUNT(*) as c FROM jobs WHERE status NOT IN ('done','failed','no_work_needed')") as cur:
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
    # LIMIT: this list re-renders on a 5s HTMX poll, and unbounded meant every
    # job ever, each with its own task-count query, every 5 seconds, forever.
    async with db.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 100") as cur:
        jobs = [dict(r) for r in await cur.fetchall()]

    # One aggregate for every job's spend beats a per-row query in the loop.
    async with db.execute("SELECT job_id, COALESCE(SUM(cost_usd), 0.0) as c FROM agents GROUP BY job_id") as cur:
        job_costs = {r["job_id"]: r["c"] for r in await cur.fetchall()}

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
                <span>${job_costs.get(job_id, 0.0):.2f}</span>
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
        async with db.execute("SELECT COUNT(*) as c FROM jobs WHERE status NOT IN ('done','failed','no_work_needed')") as cur:
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
            tokens = f"{a.get('input_tokens', 0):,} / {a.get('output_tokens', 0):,}"
            cost = f"${a.get('cost_usd', 0):.4f}" if a.get("cost_usd") else "-"
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

        # Review rounds
        review_rounds_data = await _build_review_rounds(db, job_id)
        review_rounds_html = _render_review_rounds(review_rounds_data)

        # Detail body with HTMX auto-refresh on the detail section
        detail_content = f"""
            <h2>Spec</h2>
            {spec_html}
            <h2>Tasks</h2>
            {tasks_html}
            {review_rounds_html}
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

                rows += f"<tr><td>{_esc(t['title'])}{subtask_info}</td><td>{_esc(t['service'])}</td><td>{_esc(t['agent_role'])}</td><td>{_badge(t['status'])}</td><td>{pr_link}</td><td>{err}</td></tr>"

                for s in subtasks:
                    s_err = f'<span style="color:#f44336">{_esc(str(s["error"])[:80])}</span>' if s.get("error") else ""
                    rows += f'<tr style="background:#0d1117"><td style="padding-left:32px;color:#8b949e;font-size:0.85em" colspan="3">{s["sequence_num"]}. {_esc(s["description"][:100])}</td><td>{_badge(s["status"])}</td><td colspan="2">{s_err}</td></tr>'

            parts.append(
                f"<h2>Tasks</h2><table><tr><th>Title</th><th>Service</th><th>Agent Role</th><th>Status</th><th>PR</th><th>Error</th></tr>{rows}</table>"
            )
        else:
            parts.append('<h2>Tasks</h2><p style="color:#8b949e">No tasks.</p>')

        # Review Rounds
        review_rounds_data = await _build_review_rounds(db, job_id)
        review_rounds_html = _render_review_rounds(review_rounds_data)
        if review_rounds_html:
            parts.append(review_rounds_html)

        # Agents
        async with db.execute("SELECT * FROM agents WHERE job_id = ? ORDER BY started_at", (job_id,)) as cur:
            agents = [dict(r) for r in await cur.fetchall()]
        if agents:
            rows = ""
            for a in agents:
                dur = _elapsed(a["started_at"], a["finished_at"]) if a.get("finished_at") else "running"
                log_link = f'<span style="font-family:monospace;font-size:0.85em">{_esc(a["log_file"])}</span>' if a.get("log_file") else "-"
                err = f'<span style="color:#f44336">{_esc(str(a["error"])[:100])}</span>' if a.get("error") else "-"
                tokens = f"{a.get('input_tokens', 0):,} / {a.get('output_tokens', 0):,}"
                cost = f"${a.get('cost_usd', 0):.4f}" if a.get("cost_usd") else "-"
                turns = str(a.get("num_turns", 0)) if a.get("num_turns") else "-"
                model = _esc(a.get("model") or "-")
                rows += f"<tr><td>{_esc(a['role'])}</td><td>{_badge(a['status'])}</td><td>{dur}</td><td>{tokens}</td><td>{cost}</td><td>{turns}</td><td>{model}</td><td>{log_link}</td><td>{err}</td></tr>"
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


# -- Memory Page --


@app.get("/memory", response_class=HTMLResponse)
async def memory_page():
    db = await _get_db()
    try:
        summary_html = await _render_memory_summary(db)
        ops_html = await _render_memory_operations(db)

        body = f"""
        <div id="memory-summary" hx-get="/api/memory/summary-html" hx-trigger="every 10s" hx-swap="morph:innerHTML">
        {summary_html}
        </div>

        <div class="graph-container">
            <h2>Knowledge Graph</h2>
            <div style="margin-bottom:8px">
                <select id="graph-project" class="project-select" onchange="loadGraph()">
                    <option value="">All projects</option>
                </select>
                <select id="graph-limit" class="project-select" onchange="loadGraph()">
                    <option value="100">100 nodes</option>
                    <option value="200" selected>200 nodes</option>
                    <option value="500">500 nodes</option>
                </select>
            </div>
            <div id="memory-graph"></div>
        </div>

        <div class="ops-timeline" id="ops-timeline" hx-get="/api/memory/operations-html" hx-trigger="every 10s" hx-swap="morph:innerHTML">
            <h2>Operations Timeline</h2>
            {ops_html}
        </div>

        <script src="https://unpkg.com/vis-network@9.1.9/dist/vis-network.min.js"></script>
        <script>
        let network = null;
        async function loadProjects() {{
            try {{
                const resp = await fetch('/api/memory/graph?limit=1');
                const data = await resp.json();
                const sel = document.getElementById('graph-project');
                const projects = [...new Set(data.nodes.map(n => n.project).filter(Boolean))];
                projects.forEach(p => {{
                    const opt = document.createElement('option');
                    opt.value = p;
                    opt.textContent = p;
                    sel.appendChild(opt);
                }});
            }} catch(e) {{}}
        }}
        async function loadGraph() {{
            const project = document.getElementById('graph-project').value;
            const limit = document.getElementById('graph-limit').value;
            let url = '/api/memory/graph?limit=' + limit;
            if (project) url += '&project=' + encodeURIComponent(project);
            try {{
                const resp = await fetch(url);
                const data = await resp.json();
                const container = document.getElementById('memory-graph');
                const nodes = new vis.DataSet(data.nodes);
                const edges = new vis.DataSet(data.edges);
                const options = {{
                    nodes: {{
                        shape: 'dot',
                        font: {{ color: '#c9d1d9', size: 12 }},
                        borderWidth: 2,
                    }},
                    edges: {{
                        color: {{ color: '#30363d', highlight: '#58a6ff' }},
                        font: {{ color: '#8b949e', size: 10, align: 'middle' }},
                        arrows: 'to',
                    }},
                    groups: {{
                        knowledge: {{ color: {{ background: '#2196f3', border: '#1565c0' }} }},
                        entity: {{ color: {{ background: '#e040fb', border: '#aa00ff' }}, shape: 'diamond' }},
                    }},
                    physics: {{
                        forceAtlas2Based: {{
                            gravitationalConstant: -30,
                            centralGravity: 0.005,
                            springLength: 150,
                            springConstant: 0.02,
                        }},
                        solver: 'forceAtlas2Based',
                        stabilization: {{ iterations: 100 }},
                    }},
                    interaction: {{ hover: true, tooltipDelay: 200 }},
                    layout: {{ improvedLayout: true }},
                }};
                if (network) network.destroy();
                network = new vis.Network(container, {{ nodes, edges }}, options);
            }} catch(e) {{
                document.getElementById('memory-graph').innerHTML =
                    '<p style="color:#8b949e;padding:40px;text-align:center">No memory data available (Postgres required)</p>';
            }}
        }}
        loadProjects();
        loadGraph();
        </script>"""

        return _page("Memory", body, active="memory")
    finally:
        await db.close()


async def _render_memory_summary(db) -> str:
    """Render summary cards for memory tiers."""
    try:
        async with db.execute("SELECT COUNT(*) as c FROM memory_nodes") as cur:
            node_count = (await cur.fetchone())["c"]
        async with db.execute("SELECT COUNT(*) as c FROM memory_entities") as cur:
            entity_count = (await cur.fetchone())["c"]
        async with db.execute("SELECT COUNT(*) as c FROM memory_links") as cur:
            link_count = (await cur.fetchone())["c"]

        ops_count = 0
        try:
            async with db.execute("SELECT COUNT(*) as c FROM memory_operations") as cur:
                ops_count = (await cur.fetchone())["c"]
        except Exception:
            pass

        # Per-project breakdown
        project_rows = ""
        try:
            async with db.execute("SELECT project, COUNT(*) as c FROM memory_nodes GROUP BY project ORDER BY c DESC LIMIT 10") as cur:
                projects = await cur.fetchall()
            for p in projects:
                project_rows += f'<div style="display:flex;justify-content:space-between;padding:2px 0"><span>{_esc(p["project"])}</span><span style="color:#f0f6fc;font-weight:600">{p["c"]}</span></div>'
        except Exception:
            pass

        project_section = ""
        if project_rows:
            project_section = f'<div class="tier-card"><h3>By Project</h3>{project_rows}</div>'

        return f"""<div class="memory-grid">
            <div class="tier-card tier-l3"><h3>Knowledge Nodes</h3><div class="count">{node_count}</div></div>
            <div class="tier-card tier-entity"><h3>Entities</h3><div class="count">{entity_count}</div></div>
            <div class="tier-card tier-link"><h3>Links</h3><div class="count">{link_count}</div></div>
            <div class="tier-card tier-ops"><h3>Operations</h3><div class="count">{ops_count}</div></div>
            {project_section}
        </div>"""
    except Exception:
        return '<div class="memory-grid"><div class="tier-card"><h3>Memory</h3><p style="color:#8b949e">Memory tables not available (Postgres required)</p></div></div>'


async def _render_memory_operations(db, project: str = "", limit: int = 50) -> str:
    """Render recent memory operations timeline."""
    try:
        if project:
            async with db.execute(
                "SELECT * FROM memory_operations WHERE project = ? ORDER BY created_at DESC LIMIT ?",
                (project, limit),
            ) as cur:
                ops = await cur.fetchall()
        else:
            async with db.execute(
                "SELECT * FROM memory_operations ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ) as cur:
                ops = await cur.fetchall()

        if not ops:
            return '<p style="color:#8b949e">No memory operations recorded yet.</p>'

        parts = []
        for op in ops:
            op = dict(op)
            tier = op.get("tier", "unknown")
            tier_class = tier if tier in ("l2", "l3", "retrieval", "context", "archive") else "unknown"
            ts = str(op.get("created_at", ""))[:19]
            dur = f"{op.get('duration_ms', 0):.0f}ms" if op.get("duration_ms") else ""

            detail = ""
            if op.get("details"):
                try:
                    d = json.loads(op["details"]) if isinstance(op["details"], str) else op["details"]
                    detail_parts = []
                    for k, v in d.items():
                        if isinstance(v, (list, int, float)):
                            detail_parts.append(f"{k}={v}")
                        else:
                            detail_parts.append(f"{k}={str(v)[:60]}")
                    detail = " ".join(detail_parts[:5])
                except Exception:
                    pass

            parts.append(
                f'<div class="op-row">'
                f'<span class="op-ts">{_esc(ts)}</span>'
                f'<span class="op-tier {tier_class}">{_esc(tier)}</span>'
                f'<span class="op-name">{_esc(op.get("op", ""))}</span>'
                f'<span class="op-detail">{_esc(detail)}</span>'
                f'<span class="op-dur">{_esc(dur)}</span>'
                f"</div>"
            )

        return "\n".join(parts)
    except Exception:
        return '<p style="color:#8b949e">Memory operations not available (Postgres required)</p>'


@app.get("/api/memory/summary-html", response_class=HTMLResponse)
async def api_memory_summary_html():
    """HTMX endpoint for memory summary cards refresh."""
    db = await _get_db()
    try:
        return await _render_memory_summary(db)
    finally:
        await db.close()


@app.get("/api/memory/operations-html", response_class=HTMLResponse)
async def api_memory_operations_html(project: str = "", limit: int = 50):
    """HTMX endpoint for operations timeline refresh."""
    db = await _get_db()
    try:
        return await _render_memory_operations(db, project=project, limit=limit)
    finally:
        await db.close()


@app.get("/api/memory/graph")
async def api_memory_graph(project: str = "", limit: int = 200):
    """JSON endpoint for vis.js knowledge graph visualization."""
    db = await _get_db()
    try:
        nodes = []
        edges = []

        # Fetch knowledge nodes
        if project:
            async with db.execute(
                "SELECT id, title, tags, project, content FROM memory_nodes WHERE project = ? ORDER BY created_at DESC LIMIT ?",
                (project, limit),
            ) as cur:
                node_rows = await cur.fetchall()
        else:
            async with db.execute(
                "SELECT id, title, tags, project, content FROM memory_nodes ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ) as cur:
                node_rows = await cur.fetchall()

        node_ids = set()
        for row in node_rows:
            row = dict(row)
            node_id = row["id"]
            node_ids.add(node_id)
            tags = row.get("tags", [])
            if isinstance(tags, str):
                try:
                    tags = json.loads(tags)
                except Exception:
                    tags = [tags]
            label = row.get("title") or str(row.get("content", ""))[:40]
            nodes.append(
                {
                    "id": node_id,
                    "label": label,
                    "group": "knowledge",
                    "title": f"tags: {', '.join(str(t) for t in tags)}\nproject: {row.get('project', '')}",
                    "project": row.get("project", ""),
                }
            )

        if not node_ids:
            return JSONResponse({"nodes": [], "edges": []})

        # Fetch links from those nodes to entities
        placeholders = ",".join(["?" for _ in node_ids])
        async with db.execute(
            f"SELECT from_node, to_entity, link_type, confidence FROM memory_links WHERE from_node IN ({placeholders})",
            tuple(node_ids),
        ) as cur:
            link_rows = await cur.fetchall()

        entity_ids = set()
        for row in link_rows:
            row = dict(row)
            entity_ids.add(row["to_entity"])
            edges.append(
                {
                    "from": row["from_node"],
                    "to": f"entity-{row['to_entity']}",
                    "label": row.get("link_type", ""),
                    "value": row.get("confidence", 1.0),
                }
            )

        # Fetch entity details
        if entity_ids:
            ent_placeholders = ",".join(["?" for _ in entity_ids])
            async with db.execute(
                f"SELECT id, name, entity_type, project FROM memory_entities WHERE id IN ({ent_placeholders})",
                tuple(entity_ids),
            ) as cur:
                entity_rows = await cur.fetchall()

            for row in entity_rows:
                row = dict(row)
                nodes.append(
                    {
                        "id": f"entity-{row['id']}",
                        "label": row.get("name", row["id"]),
                        "group": "entity",
                        "title": f"type: {row.get('entity_type', '?')}\nproject: {row.get('project', '')}",
                        "project": row.get("project", ""),
                    }
                )

        return JSONResponse({"nodes": nodes, "edges": edges})
    except Exception as e:
        logger.debug("Memory graph query failed: %s", e)
        return JSONResponse({"nodes": [], "edges": []})
    finally:
        await db.close()


@app.get("/api/memory/operations")
async def api_memory_operations(project: str = "", limit: int = 50):
    """JSON endpoint for memory operations."""
    db = await _get_db()
    try:
        if project:
            async with db.execute(
                "SELECT * FROM memory_operations WHERE project = ? ORDER BY created_at DESC LIMIT ?",
                (project, limit),
            ) as cur:
                ops = [dict(r) for r in await cur.fetchall()]
        else:
            async with db.execute(
                "SELECT * FROM memory_operations ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ) as cur:
                ops = [dict(r) for r in await cur.fetchall()]
        return JSONResponse(ops)
    except Exception as e:
        logger.debug("Memory operations query failed: %s", e)
        return JSONResponse([])
    finally:
        await db.close()


@app.get("/healthz")
async def healthz():
    """Liveness/readiness target. Touches nothing on purpose.

    This exists because /metrics used to be the probe target -- it was the only
    route that answered without hitting the DB. It now reads the database on
    scrape, so probing it would make LIVENESS depend on Postgres: a slow query
    on a cold cache would fail the probe and the kubelet would restart a pod
    whose only problem was a busy database. That turns a database hiccup into a
    restart loop.
    """
    return {"status": "ok"}


# -- Prometheus --
#
# These gauges are read from the DATABASE on scrape, not accumulated in this
# process. That is deliberate:
#
#   * The engine runs `strategy: Recreate`, one replica, so in-process counters
#     reset on every rollout. "Was haiku worth it last month" is unanswerable
#     from a counter that forgets each deploy. The DB already holds the durable,
#     fully-dimensioned record.
#   * It is served from the DASHBOARD rather than the engine because the engine
#     has no HTTP server at all -- `--server` exposes only the MCP SSE surface.
#     These are facts about data, not about process internals, so they do not
#     need to live where the work happens.
#
# The cost of reading on scrape is a DB round-trip per scrape, so results are
# cached: _get_db() opens a fresh connection per request with no pool, and
# pool_max is deliberately 6 across three processes. An uncached 15s scrape
# would spend a meaningful slice of the connection budget on telemetry.
_METRICS_TTL_SECONDS = 60.0
_metrics_cache: tuple[float, str] | None = None


def _metric_lines(name: str, help_text: str, samples: list[tuple[dict, float]]) -> list[str]:
    """Render one gauge family in the Prometheus text exposition format."""
    lines = [f"# HELP {name} {help_text}", f"# TYPE {name} gauge"]
    for labels, value in samples:
        if labels:
            rendered = ",".join(f'{k}="{_escape_label(v)}"' for k, v in labels.items())
            lines.append(f"{name}{{{rendered}}} {value}")
        else:
            lines.append(f"{name} {value}")
    return lines


def _escape_label(value) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


async def _render_metrics() -> str:
    """Build the exposition payload from a single pass over the DB."""
    from .db.postgres import PostgresDatabase

    config = Config.from_env()
    db = PostgresDatabase(_postgres_url, 1, 2)
    await db.connect()
    try:
        eff = await db.get_model_effectiveness(days=config.metrics_window_days, turn_ceiling=config.agent_max_turns)
        out = await db.get_outcome_breakdown(days=config.metrics_window_days)
    finally:
        await db.close()

    lines: list[str] = []
    detail = eff["rows"]
    lines += _metric_lines(
        "minion_spend_usd",
        "Agent spend in USD by model, role and job difficulty",
        [({"model": r["model"], "role": r["role"], "difficulty": r["difficulty"]}, r["spend_usd"]) for r in detail],
    )
    lines += _metric_lines(
        "minion_agent_runs",
        "Agent invocations by model, role and job difficulty",
        [({"model": r["model"], "role": r["role"], "difficulty": r["difficulty"]}, r["runs"]) for r in detail],
    )
    lines += _metric_lines(
        "minion_agent_failures",
        "Agent invocations that ended status=failed",
        [({"model": r["model"], "role": r["role"], "difficulty": r["difficulty"]}, r["failed"]) for r in detail],
    )
    lines += _metric_lines(
        "minion_agent_turn_ceiling_hits",
        "Agent runs that reached the configured turn ceiling (a SILENT failure: status stays done, error stays null)",
        # Labelled by the keys this rollup actually groups on. by_model_difficulty
        # carries no `role`, so labelling it with one is a KeyError at scrape time.
        [({"model": r["model"], "difficulty": r["difficulty"]}, r["ceiling_hits"]) for r in eff["by_model_difficulty"]],
    )
    lines += _metric_lines(
        "minion_agent_turns_avg",
        "Mean turns per agent run by model",
        [({"model": r["model"]}, r["avg_turns"]) for r in eff["by_model"]],
    )
    lines += _metric_lines(
        "minion_jobs",
        "Jobs by terminal status in the window",
        [({"status": r["status"]}, r["jobs"]) for r in out["by_status"]],
    )
    lines += _metric_lines(
        "minion_job_spend_usd",
        "Spend in USD by job status — spend against failed is spend that bought nothing",
        [({"status": r["status"]}, r["spend_usd"]) for r in out["by_status"]],
    )
    # The headline. All spend over finished jobs, so failed work is charged to
    # the successes it did not produce. Absent (not zero) when nothing finished:
    # a zero here would read as "free" on a graph.
    if out["cost_per_success_usd"] is not None:
        lines += _metric_lines(
            "minion_cost_per_success_usd",
            "Total spend divided by jobs reaching done — the fully-loaded cost of one finished job",
            [({}, out["cost_per_success_usd"])],
        )
    q = out["quality"]
    lines += _metric_lines("minion_tasks_revised", "Tasks that went through at least one revision round", [({}, q["tasks_revised"])])
    lines += _metric_lines("minion_revisions_total", "Total revision rounds across all tasks", [({}, q["revisions_total"])])
    lines += _metric_lines(
        "minion_review_verdicts",
        "Review verdicts recorded on tasks",
        [({"verdict": "approve"}, q["verdict_approve"]), ({"verdict": "request_changes"}, q["verdict_request_changes"])],
    )
    lines += _metric_lines(
        "minion_metrics_window_days",
        "Lookback window these gauges are computed over",
        [({}, out["period_days"])],
    )
    return "\n".join(lines) + "\n"


@app.get("/metrics")
async def prometheus_metrics():
    """Prometheus metrics, computed from the database and cached briefly."""
    global _metrics_cache
    from fastapi.responses import Response

    now = time.monotonic()
    if _metrics_cache is not None and now - _metrics_cache[0] < _METRICS_TTL_SECONDS:
        return Response(content=_metrics_cache[1], media_type="text/plain; version=0.0.4; charset=utf-8")

    try:
        payload = await _render_metrics()
    except Exception as e:
        # A scrape must never 500 the dashboard, and a silent empty payload
        # would read as "zero spend" on a graph. Say what broke instead.
        logger.warning("metrics render failed: %s", e, exc_info=True)
        return Response(content=f"# metrics unavailable: {_escape_label(e)[:200]}\n", media_type="text/plain")

    _metrics_cache = (now, payload)
    return Response(content=payload, media_type="text/plain; version=0.0.4; charset=utf-8")


def run_dashboard(port: int = 8322):
    """Start the dashboard web server."""
    import uvicorn

    global _postgres_url
    config = Config.from_env()
    _postgres_url = config.postgres_url

    logger.info("Starting dashboard on http://localhost:%d", port)
    logger.info("Reading from database: postgres (search_path=minions)")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
