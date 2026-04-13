"""MCP server for agent-memory — exposes memory tools to Claude Code agents.

Reads DATABASE_URL from environment (or .env in the project root).

Tools:
  log_event    — fire-and-forget event logging (tmux hooks, session tracking)
  create_note  — write a curated memory node (decisions, insights, checkpoints)
  query_notes  — search curated notes by project + tags
  recent_events — query raw event log by project + time window
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── Globals (connected at startup via lifespan) ───────────────────────────────

_pool = None
_store = None


def _db_url() -> str:
    url = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL") or ""
    if not url:
        raise RuntimeError(
            "DATABASE_URL environment variable is required. "
            "Set it in the project .env or export it before starting the server."
        )
    # Ensure SSL for hosted Postgres (Digital Ocean, etc.) unless already specified
    if "sslmode" not in url:
        sep = "&" if "?" in url else "?"
        url += f"{sep}sslmode=require"
    return url


@asynccontextmanager
async def _lifespan(server):
    """Connect to Postgres on startup, disconnect on shutdown."""
    global _pool, _store

    import psycopg_pool

    from agent_memory.backends.postgres import PostgresMemoryBackend
    from agent_memory.store import MemoryStore

    url = _db_url()
    _pool = psycopg_pool.AsyncConnectionPool(url, min_size=1, max_size=5)
    await _pool.open()

    backend = PostgresMemoryBackend(pool=_pool)
    _store = MemoryStore(backend)

    logger.info("agent-memory MCP server ready")
    yield

    await _pool.close()
    logger.info("agent-memory MCP server disconnected")


# ── Server ────────────────────────────────────────────────────────────────────

from fastmcp import FastMCP  # noqa: E402

mcp = FastMCP("agent-memory", lifespan=_lifespan)


# ── Tools ─────────────────────────────────────────────────────────────────────


@mcp.tool()
async def log_event(
    event_type: str,
    project: str,
    details: dict[str, Any] | None = None,
    device: str = "",
    repo: str = "",
    branch: str = "",
    agent_slot: str = "",
    session_id: str = "",
) -> str:
    """Log a raw event for a project — session lifecycle, tool use, errors, etc.

    This is fire-and-forget. Use it from tmux hooks or to track agent activity.
    Not for curated notes — use create_note for those.

    event_type: session_start | session_end | tool_use | permission_wait |
                file_write | commit | error | checkpoint
    details: arbitrary key/value payload (tool name, file paths, error message, etc.)
    device: machine identifier (e.g. "mac-laptop", "windows-desktop")
    repo: repository name
    branch: current git branch
    agent_slot: tmux window index (from the agent registry)
    session_id: Claude session ID if available
    """
    event_id = uuid.uuid4().hex[:12]
    async with _pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO minions.memory_events
                (id, project, event_type, device, repo, branch, agent_slot, session_id, payload)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                event_id,
                project,
                event_type,
                device,
                repo,
                branch,
                agent_slot,
                session_id or None,
                json.dumps(details or {}),
            ),
        )
    return f"logged:{event_id}"


@mcp.tool()
async def create_note(
    content: str,
    project: str,
    tags: list[str] | None = None,
    title: str = "",
    links: list[str] | None = None,
) -> str:
    """Write a curated memory note — a decision, insight, checkpoint, or finding.

    Notes are persistent and searchable. Use them to capture things worth
    remembering across sessions: architectural decisions, discovered constraints,
    completed work summaries, open questions.

    Prefer tags from the controlled vocabulary:
      Domain:  auth, api, database, frontend, backend, infra, testing, deployment
      Action:  bug, fix, refactor, feature, breaking-change, decision, investigation
      Outcome: approved, merged, deployed, reverted, wontfix

    links: entity names to link this note to (file paths, module names, repo names).
           Enables backlink queries like "everything that mentions svc-chatbot".
    """
    from agent_memory.tags import normalize_tags
    from agent_memory.types import MemoryNode

    node_id = uuid.uuid4().hex[:12]
    node = MemoryNode(
        id=node_id,
        content=content,
        title=title or None,
        tags=normalize_tags(tags or []),
        project=project,
    )
    await _store.create_node(node)

    for entity_name in links or []:
        await _store.ensure_entity(entity_name, project)
        await _store.create_link(node_id, entity_name, "mentions")

    return f"created:{node_id}"


@mcp.tool()
async def query_notes(
    project: str,
    tags: list[str] | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Search curated memory notes for a project.

    With tags: returns notes that have ANY of the specified tags (overlap match).
    Without tags: returns the most recent notes for the project.

    Results are ordered most-recent first.
    """
    if tags:
        nodes = await _store.query_by_tags(project, tags, limit=limit)
    else:
        # No tag filter — fetch all notes for project, most recent first
        async with _pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, content, title, tags, access_count, created_at
                FROM minions.memory_nodes
                WHERE project = %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (project, limit),
            )
            rows = await cur.fetchall()
            return [
                {
                    "id": r[0],
                    "content": r[1],
                    "title": r[2] or "",
                    "tags": list(r[3]) if r[3] else [],
                    "access_count": r[4] or 0,
                    "created_at": str(r[5]),
                }
                for r in rows
            ]

    return [
        {
            "id": n.id,
            "content": n.content,
            "title": n.title or "",
            "tags": n.tags,
            "access_count": n.access_count,
            "created_at": n.created_at,
        }
        for n in nodes
    ]


@mcp.tool()
async def recent_events(
    project: str,
    event_type: str | None = None,
    hours: int = 24,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Query the raw event log for a project.

    Useful for understanding recent agent activity:
      recent_events("svc-chatbot", hours=48)
      recent_events("svc-chatbot", event_type="session_start")

    event_type filter is optional. Results are newest first.
    """
    params: list[Any] = [project, hours]
    type_clause = ""
    if event_type:
        type_clause = "AND event_type = %s"
        params.append(event_type)
    params.append(limit)

    async with _pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            f"""
            SELECT id, timestamp, event_type, device, repo, branch, agent_slot, session_id, payload
            FROM minions.memory_events
            WHERE project = %s
              AND timestamp > now() - (%s * interval '1 hour')
              {type_clause}
            ORDER BY timestamp DESC
            LIMIT %s
            """,
            params,
        )
        rows = await cur.fetchall()

    return [
        {
            "id": row[0],
            "timestamp": str(row[1]),
            "event_type": row[2],
            "device": row[3] or "",
            "repo": row[4] or "",
            "branch": row[5] or "",
            "agent_slot": row[6] or "",
            "session_id": row[7] or "",
            "details": row[8] if isinstance(row[8], dict) else json.loads(row[8] or "{}"),
        }
        for row in rows
    ]


# ── Entry point ───────────────────────────────────────────────────────────────


def main():
    """Start the MCP server (stdio transport — Claude Code spawns this process)."""
    # Load .env from project root if present
    _env_path = Path(__file__).parent.parent.parent / ".env"
    if _env_path.exists():
        try:
            from dotenv import load_dotenv
            load_dotenv(_env_path)
        except ImportError:
            pass  # python-dotenv optional — fall back to env vars already set

    logging.basicConfig(level=logging.WARNING)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
