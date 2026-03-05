"""Shared fixtures for all tests."""

import pytest

from minions.core.models import AgentRole, Task
from minions.db import SQLiteDatabase


@pytest.fixture
async def db():
    """In-memory SQLite database, schema applied."""
    database = SQLiteDatabase(":memory:")
    await database.connect()
    yield database
    await database.close()


@pytest.fixture
async def sample_job(db):
    """A development job in spec_received state."""
    return await db.create_job("Implement user authentication")


@pytest.fixture
async def sample_review_job(db):
    """A review job with a CODE_REVIEWER task."""
    return await db.create_review_job(
        project="team/api",
        mr_url="https://gitlab.example.com/team/api/-/merge_requests/42",
        mr_id="42",
    )


@pytest.fixture
def make_task():
    """Factory for creating Task instances."""
    def _make(job_id, **overrides):
        defaults = {
            "job_id": job_id,
            "title": "Test task",
            "description": "A test task",
            "service": "api",
            "agent_role": AgentRole.BACKEND_ENGINEER,
        }
        defaults.update(overrides)
        return Task(**defaults)
    return _make
