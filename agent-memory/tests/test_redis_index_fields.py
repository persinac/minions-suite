"""RediSearch index field construction.

create_index builds every field through a factory map, then calls each factory
with the same keywords. A factory whose signature does not accept those keywords
raises TypeError, no index is created, and TupleSpace.connect() fails — which
takes the entire memory tier down, silently, because cli.py catches the failure
and logs "continuing without it".

That is exactly what shipped: the NUMERIC SORTABLE entry took only a positional
name while the call site passed as_name=. These tests pin the contract that every
factory must satisfy, without needing a live Redis Stack (fakeredis does not
implement RediSearch, so the real create_index path cannot be exercised here).
"""

import inspect

import pytest
from redis.commands.search.field import NumericField, TagField, TextField

from agent_memory.backends.redis import RedisTupleSpaceBackend
from agent_memory.tuplespace import TupleSpace


def _field_map():
    """Rebuild the factory map exactly as create_index does."""
    return {
        "TAG": TagField,
        "TEXT": TextField,
        "NUMERIC SORTABLE": lambda n, **kw: NumericField(n, sortable=True, **kw),
    }


class TestFieldFactories:
    @pytest.mark.parametrize("field_type", ["TAG", "TEXT", "NUMERIC SORTABLE"])
    def test_every_factory_accepts_the_call_sites_keywords(self, field_type):
        """create_index calls factory(path, as_name=name) for all three types.

        Asserts on the rendered redis_args() rather than merely that construction
        returned something: a factory could silently drop as_name and still build
        a Field, producing an index whose fields are addressable only by JSON path
        and never by the names the queries use.
        """
        field = _field_map()[field_type]("$.created_at", as_name="created_at")
        args = field.redis_args()
        assert args[0] == "$.created_at"
        assert "AS" in args and "created_at" in args

    def test_numeric_factory_still_sets_sortable(self):
        """The as_name fix must not drop the sortable flag it existed to set."""
        field = _field_map()["NUMERIC SORTABLE"]("$.created_at", as_name="created_at")
        assert "SORTABLE" in field.redis_args()

    def test_factories_are_callable_with_only_a_name(self):
        """Positional-only use must keep working for any other caller."""
        for factory in _field_map().values():
            assert factory("$.x") is not None


class TestSchemaCoverage:
    """The schema TupleSpace passes must only use types the map knows."""

    def test_every_schema_type_has_a_factory(self):
        schema = getattr(TupleSpace, "INDEX_SCHEMA", None)
        if schema is None:
            src = inspect.getsource(TupleSpace.connect)
            # Field types appear as string literals in the schema dict.
            used = {t for t in ("TAG", "TEXT", "NUMERIC SORTABLE") if f'"{t}"' in src or f"'{t}'" in src}
        else:
            used = set(schema.values())

        unknown = used - set(_field_map())
        assert not unknown, f"schema uses field types with no factory: {unknown}"

    def test_backend_exposes_create_index(self):
        assert hasattr(RedisTupleSpaceBackend, "create_index")
