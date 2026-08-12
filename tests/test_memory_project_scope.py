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
from agent_memory.tuplespace import TupleSpace, _escape_tag


class _RecordingBackend:
    """Captures what actually reached the backend, keys included."""

    def __init__(self):
        self.puts = []
        self.queries = []

    async def connect(self):
        return None

    async def close(self):
        return None

    async def create_index(self, name, schema):
        return None

    async def put(self, key, doc, ttl=None):
        self.puts.append((key, doc))
        return key

    async def search(self, index, query, limit=20):
        self.queries.append(query)
        return []


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


class TestReadScope:
    """The read half. Fixing writes alone is worse than fixing neither: facts
    land under the right project while queries still ask the startup scope, so
    a correctly-written fact reads back as absent.

    Queries carry the project as a RediSearch TAG, where `-` is a separator and
    so arrives escaped (`playfield\\-relay`). Assert against `_escape_tag` rather
    than the raw name, or a `not in` check passes for the wrong reason.
    """

    @pytest.mark.asyncio
    async def test_a_query_asks_the_job_s_project(self, space, backend):
        await space.rd(category="decision", project="playfield-relay")

        assert _escape_tag("playfield-relay") in backend.queries[0]
        assert _escape_tag("flashback-android") not in backend.queries[0]

    @pytest.mark.asyncio
    async def test_omitting_project_still_uses_the_instance_scope(self, space, backend):
        await space.rd(category="decision")

        assert _escape_tag("flashback-android") in backend.queries[0]

    @pytest.mark.asyncio
    async def test_empty_string_falls_back_rather_than_querying_a_blank_scope(self, space, backend):
        await space.rd(category="decision", project="")

        assert _escape_tag("flashback-android") in backend.queries[0]

    @pytest.mark.asyncio
    async def test_a_write_is_readable_through_the_matching_query(self, space, backend):
        """Both halves must agree on the namespace, which is the whole point."""
        await space.out(category="decision", key="task_plan_created", value="v", project="playfield-relay")
        await space.rd(category="decision", project="playfield-relay")

        written_key, doc = backend.puts[0]
        assert _escape_tag(doc["project"]) in backend.queries[0]
        assert "playfield-relay" in written_key


class TestCallSite:
    """Every tool that takes a `project` must hand it to the tuplespace.

    Source inspection rather than invocation: these are FastMCP closures built
    inside create_server, so there is no importable function object to call.
    """

    def _tool_body(self, name: str) -> str:
        import inspect

        from minions.server import mcp as mcp_module

        source = inspect.getsource(mcp_module)
        start = source.index(f"async def {name}")
        return source[start : start + 1600]

    def test_create_memory_note_forwards_its_project_argument(self):
        """The original regression: the parameter existed and was never used."""
        assert "project=project" in self._tool_body("create_memory_note"), "create_memory_note drops its project argument"

    def test_publish_fact_forwards_its_project_argument(self):
        """Job 68576a15 (playfield-relay) filed decision:task_plan_created under flashback-android."""
        body = self._tool_body("publish_fact")
        out_call = body[body.index("tuplespace.out(") :]

        assert "project=project" in out_call, "publish_fact drops its project argument"

    def test_query_facts_forwards_its_project_argument(self):
        body = self._tool_body("query_facts")
        rd_call = body[body.index("tuplespace.rd(") :]

        assert "project=project" in rd_call, "query_facts drops its project argument"

    def test_publish_fact_does_not_merely_echo_the_project_back(self):
        """It returned {"project": project} in its response the whole time it was
        discarding it, so the agent saw a success that named the right project."""
        body = self._tool_body("publish_fact")

        assert body.index("tuplespace.out(") < body.index('"project": project'), "the only use of `project` is the echoed response"
