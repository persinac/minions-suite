"""FastMCP server exposing review management tools.

Provides external integrations (other MCP clients, dashboards) a way
to trigger reviews, query status, and inspect costs.
"""

import json
import logging
from typing import Optional

from fastmcp import FastMCP

from .config import Config
from .db import AbstractDatabase
from .models import Review, ReviewStatus

logger = logging.getLogger(__name__)


def create_server(db: AbstractDatabase, config: Optional[Config] = None) -> FastMCP:
    """Create and return the FastMCP server with review tools."""
    mcp = FastMCP("Minion Suite", instructions="AI agent suite — composable, vendor-agnostic agents. Currently: code review.")

    # =========================================================================
    # Review Tools
    # =========================================================================

    @mcp.tool()
    async def request_review(project: str, mr_url: str, mr_id: str) -> str:
        """Queue a new review for a merge/pull request."""
        review = Review(project=project, mr_url=mr_url, mr_id=mr_id)
        review = await db.create_review(review)
        return json.dumps({"review_id": review.id, "status": review.status, "project": project})

    @mcp.tool()
    async def get_review_status(review_id: str) -> str:
        """Get the current status of a review."""
        review = await db.get_review(review_id)
        if not review:
            return json.dumps({"error": f"Review {review_id} not found"})
        return json.dumps({
            "review_id": review.id,
            "project": review.project,
            "mr_url": review.mr_url,
            "status": review.status,
            "verdict": review.verdict,
            "comments_posted": review.comments_posted,
            "error": review.error,
        })

    @mcp.tool()
    async def get_review_history(project: Optional[str] = None, limit: int = 20) -> str:
        """Get recent review history, optionally filtered by project."""
        reviews = await db.get_reviews(project=project, limit=limit)
        return json.dumps([
            {
                "id": r.id,
                "project": r.project,
                "mr_url": r.mr_url,
                "status": r.status,
                "verdict": r.verdict,
                "comments_posted": r.comments_posted,
                "created_at": r.created_at,
                "completed_at": r.completed_at,
            }
            for r in reviews
        ])

    @mcp.tool()
    async def cancel_review(review_id: str) -> str:
        """Cancel a queued review (cannot cancel in-progress reviews)."""
        review = await db.get_review(review_id)
        if not review:
            return json.dumps({"error": f"Review {review_id} not found"})
        if review.status != ReviewStatus.QUEUED:
            return json.dumps({"error": f"Can only cancel queued reviews (current: {review.status})"})
        await db.update_review(review_id, status=ReviewStatus.FAILED, error="Cancelled by user")
        return json.dumps({"review_id": review_id, "status": "cancelled"})

    # =========================================================================
    # Cost & Stats Tools
    # =========================================================================

    @mcp.tool()
    async def get_cost_summary(project: Optional[str] = None, days: int = 30) -> str:
        """Get cost and usage summary for reviews over a time period."""
        summary = await db.get_cost_summary(project=project, days=days)
        return json.dumps(summary)

    @mcp.tool()
    async def get_agent_logs(review_id: str) -> str:
        """List agent invocations for a review with their metrics."""
        agents = await db.get_agents(review_id)
        return json.dumps([
            {
                "id": a.id,
                "model": a.model,
                "status": a.status,
                "input_tokens": a.input_tokens,
                "output_tokens": a.output_tokens,
                "cost_usd": a.cost_usd,
                "num_turns": a.num_turns,
                "log_file": a.log_file,
                "started_at": a.started_at,
                "finished_at": a.finished_at,
                "error": a.error,
            }
            for a in agents
        ])

    # =========================================================================
    # Resources
    # =========================================================================

    @mcp.resource("review://{review_id}")
    async def review_resource(review_id: str) -> str:
        """Get full review details including comments."""
        review = await db.get_review(review_id)
        if not review:
            return json.dumps({"error": "Not found"})
        comments = await db.get_comments(review_id)
        agents = await db.get_agents(review_id)
        return json.dumps({
            "review": review.model_dump(),
            "comments": [c.model_dump() for c in comments],
            "agents": [a.model_dump() for a in agents],
        })

    return mcp
