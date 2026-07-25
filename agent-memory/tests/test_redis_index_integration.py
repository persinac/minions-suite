"""End-to-end index creation against a real Redis Stack.

The unit tests around the field-factory map could not catch either of the two
bugs that kept the memory tier dead in production:

  1. create_index called factory(path, as_name=...) against a lambda that took
     only a positional name -> TypeError on the first numeric field
  2. it imported redis.commands.search.indexDefinition, a camelCase module path
     redis-py removed by 7.x -> ModuleNotFoundError

Both live inside create_index, and the second is a *lazy* import inside the
`except` branch — so it only fires when an index genuinely has to be created,
i.e. against a fresh Redis, which is exactly the first-run path in production and
the one no test covered.

fakeredis does not implement RediSearch, so the real path needs a real server:

    docker run -d --name redis-test -p 6399:6379 \\
      -e REDIS_ARGS="--requirepass testpw" redis/redis-stack-server:7.4.0-v8
    REDIS_TEST_URL=redis://localhost:6399 REDIS_TEST_PASSWORD=testpw pytest agent-memory/tests

Without those vars the integration tests skip loudly rather than silently pass.
"""

import importlib
import os

import pytest

from agent_memory.backends.redis import RedisTupleSpaceBackend
from agent_memory.tuplespace import TupleSpace

REDIS_URL = os.getenv("REDIS_TEST_URL")
REDIS_PASSWORD = os.getenv("REDIS_TEST_PASSWORD")

requires_redis = pytest.mark.skipif(
    not REDIS_URL,
    reason="set REDIS_TEST_URL (and REDIS_TEST_PASSWORD) to a Redis Stack to run index integration tests",
)


class TestLazyImportsResolve:
    """No server needed — catches the module-rename class of bug outright.

    create_index imports these lazily, inside branches that only execute on a
    fresh Redis. A rename in redis-py therefore goes unnoticed until first run in
    a new environment. Importing them eagerly here fails fast instead.
    """

    @pytest.mark.parametrize(
        "module,names",
        [
            ("redis.commands.search.field", ["NumericField", "TagField", "TextField"]),
            ("redis.commands.search.index_definition", ["IndexDefinition", "IndexType"]),
        ],
    )
    def test_module_and_symbols_exist(self, module, names):
        mod = importlib.import_module(module)
        missing = [n for n in names if not hasattr(mod, n)]
        assert not missing, f"{module} is missing {missing} — redis-py may have renamed them"

    def test_source_imports_the_current_paths(self):
        """Guard against the camelCase path creeping back in."""
        import inspect

        src = inspect.getsource(RedisTupleSpaceBackend.create_index)
        assert "indexDefinition" not in src, "camelCase module path was removed in redis-py 7.x"
        assert "index_definition" in src


@requires_redis
class TestAgainstRealRedis:
    async def _backend(self) -> RedisTupleSpaceBackend:
        return RedisTupleSpaceBackend(url=REDIS_URL, password=REDIS_PASSWORD or None)

    async def test_connect_creates_the_index(self):
        """The exact path that failed in production, both times."""
        ts = TupleSpace(await self._backend(), project="pytest-verify")
        await ts.connect()

        backend = ts._backend
        indexes = await backend._client.execute_command("FT._LIST")
        names = [i.decode() if isinstance(i, bytes) else i for i in indexes]
        assert "facts" in names

    async def test_connect_is_idempotent(self):
        """Second start must reuse the existing index, not error on it."""
        for _ in range(2):
            ts = TupleSpace(await self._backend(), project="pytest-verify")
            await ts.connect()

    async def test_write_then_search_round_trip(self):
        """Proves the index is queryable by field name, not just that it exists.

        A factory silently dropping as_name would still create an index — one
        addressable only by JSON path, so every search returns empty. The backend
        swallows search errors and returns [], so that failure looks identical to
        "no results" rather than surfacing.
        """
        ts = TupleSpace(await self._backend(), project="pytest-verify")
        await ts.connect()

        await ts.out(category="test-fact", key="rt-1", value="round trip", tags=["pytest"])
        results = await ts.rd(category="test-fact")

        assert results, "index exists but returns nothing — field names are likely not addressable"
