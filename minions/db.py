"""Database protocol and SQLite implementation for review history."""

import json
import logging
from typing import List, Optional, Protocol, runtime_checkable

import aiosqlite

from .models import Agent, Review, ReviewComment, ReviewStatus, _now

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Abstract protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class AbstractDatabase(Protocol):
    """Database interface for review persistence."""

    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    # Reviews
    async def create_review(self, review: Review) -> Review: ...

    async def get_review(self, review_id: str) -> Optional[Review]: ...

    async def get_reviews(self, project: Optional[str] = None, limit: int = 50) -> List[Review]: ...

    async def get_queued_reviews(self, limit: int = 10) -> List[Review]: ...

    async def update_review(self, review_id: str, **kwargs) -> Optional[Review]: ...

    # Agents
    async def create_agent(self, agent: Agent) -> Agent: ...

    async def update_agent(self, agent_id: str, **kwargs) -> None: ...

    async def get_agents(self, review_id: str) -> List[Agent]: ...

    # Comments (tracking only — actual posting goes through git provider)
    async def create_comment(self, comment: ReviewComment) -> ReviewComment: ...

    async def get_comments(self, review_id: str) -> List[ReviewComment]: ...

    # Stats
    async def get_cost_summary(self, project: Optional[str] = None, days: int = 30) -> dict: ...


# ---------------------------------------------------------------------------
# SQLite implementation
# ---------------------------------------------------------------------------


SCHEMA = """
CREATE TABLE IF NOT EXISTS reviews (
    id TEXT PRIMARY KEY,
    project TEXT NOT NULL,
    mr_url TEXT NOT NULL,
    mr_id TEXT NOT NULL,
    branch TEXT,
    title TEXT,
    author TEXT,
    status TEXT NOT NULL DEFAULT 'queued',
    verdict TEXT,
    summary TEXT,
    comments_posted INTEGER DEFAULT 0,
    model TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    review_id TEXT NOT NULL REFERENCES reviews(id),
    model TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'starting',
    started_at TEXT NOT NULL,
    finished_at TEXT,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0.0,
    num_turns INTEGER DEFAULT 0,
    log_file TEXT,
    error TEXT
);

CREATE TABLE IF NOT EXISTS review_comments (
    id TEXT PRIMARY KEY,
    review_id TEXT NOT NULL REFERENCES reviews(id),
    file_path TEXT NOT NULL,
    line INTEGER,
    severity TEXT DEFAULT 'nit',
    body TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reviews_status ON reviews(status);
CREATE INDEX IF NOT EXISTS idx_reviews_project ON reviews(project);
CREATE INDEX IF NOT EXISTS idx_agents_review ON agents(review_id);
CREATE INDEX IF NOT EXISTS idx_comments_review ON review_comments(review_id);
"""


class SQLiteDatabase:
    """Async SQLite database for local development."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        await self._db.commit()
        logger.info("SQLite connected: %s", self.db_path)

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    # -- Reviews --

    async def create_review(self, review: Review) -> Review:
        await self._db.execute(
            """INSERT INTO reviews (id, project, mr_url, mr_id, branch, title, author, status, model, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (review.id, review.project, review.mr_url, review.mr_id, review.branch, review.title, review.author, review.status, review.model, review.created_at),
        )
        await self._db.commit()
        return review

    async def get_review(self, review_id: str) -> Optional[Review]:
        cursor = await self._db.execute("SELECT * FROM reviews WHERE id = ?", (review_id,))
        row = await cursor.fetchone()
        if not row:
            return None
        return _row_to_review(row)

    async def get_reviews(self, project: Optional[str] = None, limit: int = 50) -> List[Review]:
        if project:
            cursor = await self._db.execute(
                "SELECT * FROM reviews WHERE project = ? ORDER BY created_at DESC LIMIT ?",
                (project, limit),
            )
        else:
            cursor = await self._db.execute(
                "SELECT * FROM reviews ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
        rows = await cursor.fetchall()
        return [_row_to_review(r) for r in rows]

    async def get_queued_reviews(self, limit: int = 10) -> List[Review]:
        cursor = await self._db.execute(
            "SELECT * FROM reviews WHERE status = 'queued' ORDER BY created_at ASC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [_row_to_review(r) for r in rows]

    async def update_review(self, review_id: str, **kwargs) -> Optional[Review]:
        if not kwargs:
            return await self.get_review(review_id)
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        values = list(kwargs.values()) + [review_id]
        await self._db.execute(f"UPDATE reviews SET {sets} WHERE id = ?", values)
        await self._db.commit()
        return await self.get_review(review_id)

    # -- Agents --

    async def create_agent(self, agent: Agent) -> Agent:
        await self._db.execute(
            """INSERT INTO agents (id, review_id, model, status, started_at, log_file)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (agent.id, agent.review_id, agent.model, agent.status, agent.started_at, agent.log_file),
        )
        await self._db.commit()
        return agent

    async def update_agent(self, agent_id: str, **kwargs) -> None:
        if not kwargs:
            return
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        values = list(kwargs.values()) + [agent_id]
        await self._db.execute(f"UPDATE agents SET {sets} WHERE id = ?", values)
        await self._db.commit()

    async def get_agents(self, review_id: str) -> List[Agent]:
        cursor = await self._db.execute(
            "SELECT * FROM agents WHERE review_id = ? ORDER BY started_at",
            (review_id,),
        )
        rows = await cursor.fetchall()
        return [_row_to_agent(r) for r in rows]

    # -- Comments --

    async def create_comment(self, comment: ReviewComment) -> ReviewComment:
        await self._db.execute(
            """INSERT INTO review_comments (id, review_id, file_path, line, severity, body, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (comment.id, comment.review_id, comment.file_path, comment.line, comment.severity, comment.body, comment.created_at),
        )
        await self._db.commit()
        return comment

    async def get_comments(self, review_id: str) -> List[ReviewComment]:
        cursor = await self._db.execute(
            "SELECT * FROM review_comments WHERE review_id = ? ORDER BY created_at",
            (review_id,),
        )
        rows = await cursor.fetchall()
        return [_row_to_comment(r) for r in rows]

    # -- Stats --

    async def get_cost_summary(self, project: Optional[str] = None, days: int = 30) -> dict:
        base_query = """
            SELECT
                COUNT(DISTINCT r.id) as total_reviews,
                COALESCE(SUM(a.cost_usd), 0) as total_cost,
                COALESCE(SUM(a.input_tokens), 0) as total_input_tokens,
                COALESCE(SUM(a.output_tokens), 0) as total_output_tokens,
                COALESCE(AVG(a.cost_usd), 0) as avg_cost_per_review
            FROM reviews r
            LEFT JOIN agents a ON a.review_id = r.id
            WHERE r.created_at >= datetime('now', ?)
        """
        params: list = [f"-{days} days"]
        if project:
            base_query += " AND r.project = ?"
            params.append(project)

        cursor = await self._db.execute(base_query, params)
        row = await cursor.fetchone()
        return {
            "total_reviews": row["total_reviews"],
            "total_cost_usd": round(row["total_cost"], 4),
            "total_input_tokens": row["total_input_tokens"],
            "total_output_tokens": row["total_output_tokens"],
            "avg_cost_per_review": round(row["avg_cost_per_review"], 4),
            "period_days": days,
        }


# ---------------------------------------------------------------------------
# Row helpers
# ---------------------------------------------------------------------------


def _row_to_review(row) -> Review:
    return Review(
        id=row["id"],
        project=row["project"],
        mr_url=row["mr_url"],
        mr_id=row["mr_id"],
        branch=row["branch"],
        title=row["title"],
        author=row["author"],
        status=ReviewStatus(row["status"]),
        verdict=row["verdict"],
        summary=row["summary"],
        comments_posted=row["comments_posted"] or 0,
        model=row["model"],
        error=row["error"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
    )


def _row_to_agent(row) -> Agent:
    return Agent(
        id=row["id"],
        review_id=row["review_id"],
        model=row["model"],
        status=row["status"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        input_tokens=row["input_tokens"] or 0,
        output_tokens=row["output_tokens"] or 0,
        cost_usd=row["cost_usd"] or 0.0,
        num_turns=row["num_turns"] or 0,
        log_file=row["log_file"],
        error=row["error"],
    )


def _row_to_comment(row) -> ReviewComment:
    return ReviewComment(
        id=row["id"],
        review_id=row["review_id"],
        file_path=row["file_path"],
        line=row["line"],
        severity=row["severity"],
        body=row["body"],
        created_at=row["created_at"],
    )
