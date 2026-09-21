"""Concurrent suites must not truncate each other: one shared schema on :5434
gave two worktrees 140 and 210 phantom failures, all passing in isolation."""

from pathlib import Path

from tests.conftest import TEST_SCHEMA, _schema_for


class TestEachCheckoutGetsItsOwnSchema:
    def test_two_checkouts_do_not_collide(self):
        main = _schema_for(Path("/home/persinac/repos/personal/minions-suite/tests"))
        peer = _schema_for(Path("/home/persinac/repos/.worktrees/personal_minions-suite--jevai/tests"))

        assert main != peer, "two worktrees sharing a schema is the whole bug"

    def test_the_same_checkout_is_stable_across_runs(self):
        """A schema that changed per run would leak one per invocation."""
        path = Path("/home/persinac/repos/personal/minions-suite/tests")

        assert _schema_for(path) == _schema_for(path)

    def test_the_name_is_a_usable_postgres_identifier(self):
        name = _schema_for(Path("/some/very/deeply/nested/checkout/that/goes/on/a/while/tests"))

        assert len(name) <= 63, "postgres truncates past 63 and two checkouts could then collide"
        assert name.replace("_", "").isalnum()
        assert not name[0].isdigit(), "an identifier starting with a digit needs quoting"

    def test_it_still_announces_itself_as_a_test_schema(self):
        """Anyone reading \\dn on the shared box should see what these are."""
        assert _schema_for(Path("/x/tests")).startswith("minions_test_")

    def test_the_live_value_is_derived_not_the_old_literal(self):
        """Guards the wiring, not just the helper: TEST_SCHEMA must actually use it."""
        assert TEST_SCHEMA != "minions_test", "the shared literal is what collided"
        assert TEST_SCHEMA.startswith("minions_test_")
