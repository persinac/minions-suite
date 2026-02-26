"""PostgreSQL implementation of the review database.

Uses psycopg3 with an async connection pool, following the same patterns
as mcp-minions/db_postgres.py.
"""

import json
import logging
from typing import List, Optional

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from .models import Agent, Review, ReviewComment, ReviewStatus, _now

logger = logging.getLogger(__name__)

SCHEMA_PREFIX = "reviewer"  # All tables live under reviewer.* schema


class PostgresDatabase:
    """Async PostgreSQL database for production use."""

    def __init__(self, postgres_url: str, pool_min: int = 2, pool_max: int = 10):
        self._url = postgres_url
        self._pool_min = pool_min
        self._pool_max = pool_max
        self._pool: Optional[AsyncConnectionPool] = None

    async def connect(self) -> None:
        self._pool = AsyncConnectionPool(
            conninfo=self._url,
            min_size=self._pool_min,
            max_size=self._pool_max,
            kwargs={"row_factory": dict_row},
        )
        await self._pool.open()
        logger.info("PostgresDatabase pool opened (min=%d, max=%d)", self._pool_min, self._pool_max)

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()
            self._pool = None
            logger.info("PostgresDatabase pool closed")

    # -- Reviews --

    async def create_review(self, review: Review) -> Review:
        async with self._pool.connection() as conn:
            await conn.execute(
                f"""INSERT INTO {SCHEMA_PREFIX}.reviews
                    (id, project, mr_url, mr_id, branch, title, author, status, model, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (review.id, review.project, review.mr_url, review.mr_id, review.branch, review.title, review.author, review.status, review.model, review.created_at),
            )
        return review

    async def get_review(self, review_id: str) -> Optional[Review]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT * FROM {SCHEMA_PREFIX}.reviews WHERE id = %s",
                (review_id,),
            )
            row = await cur.fetchone()
            if not row:
                return None
            return _dict_to_review(row)

    async def get_reviews(self, project: Optional[str] = None, limit: int = 50) -> List[Review]:
        async with self._pool.connection() as conn:
            if project:
                cur = await conn.execute(
                    f"SELECT * FROM {SCHEMA_PREFIX}.reviews WHERE project = %s ORDER BY created_at DESC LIMIT %s",
                    (project, limit),
                )
            else:
                cur = await conn.execute(
                    f"SELECT * FROM {SCHEMA_PREFIX}.reviews ORDER BY created_at DESC LIMIT %s",
                    (limit,),
                )
            rows = await cur.fetchall()
            return [_dict_to_review(r) for r in rows]

    async def get_queued_reviews(self, limit: int = 10) -> List[Review]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT * FROM {SCHEMA_PREFIX}.reviews WHERE status = 'queued' ORDER BY created_at ASC LIMIT %s",
                (limit,),
            )
            rows = await cur.fetchall()
            return [_dict_to_review(r) for r in rows]

    async def update_review(self, review_id: str, **kwargs) -> Optional[Review]:
        if not kwargs:
            return await self.get_review(review_id)
        sets = ", ".join(f"{k} = %s" for k in kwargs)
        values = list(kwargs.values()) + [review_id]
        async with self._pool.connection() as conn:
            await conn.execute(
                f"UPDATE {SCHEMA_PREFIX}.reviews SET {sets} WHERE id = %s",
                values,
            )
        return await self.get_review(review_id)

    # -- Agents --

    async def create_agent(self, agent: Agent) -> Agent:
        async with self._pool.connection() as conn:
            await conn.execute(
                f"""INSERT INTO {SCHEMA_PREFIX}.agents
                    (id, review_id, model, status, started_at, log_file)
                    VALUES (%s, %s, %s, %s, %s, %s)""",
                (agent.id, agent.review_id, agent.model, agent.status, agent.started_at, agent.log_file),
            )
        return agent

    async def update_agent(self, agent_id: str, **kwargs) -> None:
        if not kwargs:
            return
        sets = ", ".join(f"{k} = %s" for k in kwargs)
        values = list(kwargs.values()) + [agent_id]
        async with self._pool.connection() as conn:
            await conn.execute(
                f"UPDATE {SCHEMA_PREFIX}.agents SET {sets} WHERE id = %s",
                values,
            )

    async def get_agents(self, review_id: str) -> List[Agent]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT * FROM {SCHEMA_PREFIX}.agents WHERE review_id = %s ORDER BY started_at",
                (review_id,),
            )
            rows = await cur.fetchall()
            return [_dict_to_agent(r) for r in rows]

    # -- Comments --

    async def create_comment(self, comment: ReviewComment) -> ReviewComment:
        async with self._pool.connection() as conn:
            await conn.execute(
                f"""INSERT INTO {SCHEMA_PREFIX}.review_comments
                    (id, review_id, file_path, line, severity, body, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (comment.id, comment.review_id, comment.file_path, comment.line, comment.severity, comment.body, comment.created_at),
            )
        return comment

    async def get_comments(self, review_id: str) -> List[ReviewComment]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT * FROM {SCHEMA_PREFIX}.review_comments WHERE review_id = %s ORDER BY created_at",
                (review_id,),
            )
            rows = await cur.fetchall()
            return [_dict_to_comment(r) for r in rows]

    # -- Stats --

    async def get_cost_summary(self, project: Optional[str] = None, days: int = 30) -> dict:
        query = f"""
            SELECT
                COUNT(DISTINCT r.id) as total_reviews,
                COALESCE(SUM(a.cost_usd), 0) as total_cost,
                COALESCE(SUM(a.input_tokens), 0) as total_input_tokens,
                COALESCE(SUM(a.output_tokens), 0) as total_output_tokens,
                COALESCE(AVG(a.cost_usd), 0) as avg_cost_per_review
            FROM {SCHEMA_PREFIX}.reviews r
            LEFT JOIN {SCHEMA_PREFIX}.agents a ON a.review_id = r.id
            WHERE r.created_at >= NOW() - INTERVAL '%s days'
        """
        params = [days]
        if project:
            query += f" AND r.project = %s"
            params.append(project)

        async with self._pool.connection() as conn:
            cur = await conn.execute(query, params)
            row = await cur.fetchone()
            return {
                "total_reviews": row["total_reviews"],
                "total_cost_usd": round(float(row["total_cost"]), 4),
                "total_input_tokens": row["total_input_tokens"],
                "total_output_tokens": row["total_output_tokens"],
                "avg_cost_per_review": round(float(row["avg_cost_per_review"]), 4),
                "period_days": days,
            }


# ---------------------------------------------------------------------------
# Dict helpers
# ---------------------------------------------------------------------------


def _dict_to_review(d: dict) -> Review:
    return Review(
        id=d["id"],
        project=d["project"],
        mr_url=d["mr_url"],
        mr_id=d["mr_id"],
        branch=d.get("branch"),
        title=d.get("title"),
        author=d.get("author"),
        status=ReviewStatus(d["status"]),
        verdict=d.get("verdict"),
        summary=d.get("summary"),
        comments_posted=d.get("comments_posted", 0),
        model=d.get("model"),
        error=d.get("error"),
        created_at=d["created_at"],
        started_at=d.get("started_at"),
        completed_at=d.get("completed_at"),
    )


def _dict_to_agent(d: dict) -> Agent:
    return Agent(
        id=d["id"],
        review_id=d["review_id"],
        model=d["model"],
        status=d["status"],
        started_at=d["started_at"],
        finished_at=d.get("finished_at"),
        input_tokens=d.get("input_tokens", 0),
        output_tokens=d.get("output_tokens", 0),
        cost_usd=d.get("cost_usd", 0.0),
        num_turns=d.get("num_turns", 0),
        log_file=d.get("log_file"),
        error=d.get("error"),
    )


def _dict_to_comment(d: dict) -> ReviewComment:
    return ReviewComment(
        id=d["id"],
        review_id=d["review_id"],
        file_path=d["file_path"],
        line=d.get("line"),
        severity=d.get("severity", "nit"),
        body=d["body"],
        created_at=d["created_at"],
    )
