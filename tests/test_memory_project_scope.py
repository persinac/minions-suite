"""A memory note must be filed under the project whose job wrote it.

Observed on job 0f90844d, a management-api job:

    [L2] PUT memory_note:note-0f90844d project=flashback-android

The server builds ONE TupleSpace at startup, scoped to the first key in
projects.yaml (`cli.py`: `first_project = next(iter(projects.keys()), "default")`),
and every job shares it. `create_memory_note` accepted a `project` argument,
documented it, and had it mapped in `mcp_executor.py` -- then never passed it on,
so the startup default won every time.

This is quiet rather than loud: reads for the project that actually did the work
come back empty, and `backends/redis.py` swallows search errors and returns `[]`,
so a wrongly-scoped namespace is indistinguishable from "nothing learned yet".

Same shape as the `_resolve_service` collision that made a wallet-api job clone
Flashback-Android -- a first-match default standing in for a real per-job lookup,
with Flashback-Android winning because it sorts first.
"""

import pytest
from agent_memory.tuplespace import TupleSpace


class _RecordingBackend:
    """Captures what actually reached the backend, keys included."""

    def __init__(self):
        self.puts = []

    async def connect(self):
        return None

    async def close(self):
        return None

    async def create_index(self, name, schema):
        return None

    async def put(self, key, doc, ttl=None):
        self.puts.append((key, doc))
        return key


@pytest.fixture
def backend():
    return _RecordingBackend()


@pytest.fixture
def space(backend):
    # "flashback-android" is the real first key in projects.yaml, and so the
    # real startup scope for every job on the fleet.
    return TupleSpace(backend, project="flashback-android")


class TestProjectOverride:
    @pytest.mark.asyncio
    async def test_a_note_is_filed_under_the_job_s_project(self, space, backend):
        await space.out(
            category="memory_note",
            key="note-0f90844d",
            value="gated docs behind ENABLE_DOCS",
            project="management-api",
            job_id="0f90844d",
        )

        _, doc = backend.puts[0]
        assert doc["project"] == "management-api"

    @pytest.mark.asyncio
    async def test_the_redis_key_is_scoped_too(self, space, backend):
        """The doc field and the key must agree, or reads miss what writes stored."""
        await space.out(
            category="memory_note",
            key="note-0f90844d",
            value="x",
            project="management-api",
        )

        key, _ = backend.puts[0]
        assert "management-api" in key
        assert "flashback-android" not in key

    @pytest.mark.asyncio
    async def test_omitting_project_still_uses_the_instance_scope(self, space, backend):
        """Backwards compatible: every existing caller passes no project."""
        await space.out(category="memory_note", key="k", value="v")

        _, doc = backend.puts[0]
        assert doc["project"] == "flashback-android"

    @pytest.mark.asyncio
    async def test_empty_string_falls_back_rather_than_writing_a_blank_scope(self, space, backend):
        """A blank project must not become a namespace of its own."""
        await space.out(category="memory_note", key="k", value="v", project="")

        _, doc = backend.puts[0]
        assert doc["project"] == "flashback-android"


class TestCallSite:
    def test_create_memory_note_forwards_its_project_argument(self):
        """The regression itself: the parameter existed and was never used."""
        import inspect

        from minions.server import mcp as mcp_module

        source = inspect.getsource(mcp_module)
        start = source.index("async def create_memory_note")
        body = source[start : start + 1200]

        assert "project=project" in body, "create_memory_note drops its project argument"
