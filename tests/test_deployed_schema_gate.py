"""The release gate that would have caught the 0.8.31 incident.

0.8.31 shipped `update_job_spec` writing `jobs.original_spec` while the deployed
database had no such column -- the migration had only been applied to the local
test container. Every new development job died at the spec_ready transition,
silently, for forty minutes.

The deploy verification that ran at the time checked that the new CODE was live
in the pod. It was. Nobody asked whether the SCHEMA that code depends on was
live. These are different questions and only one was being asked.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_deployed_schema.py"
MIGRATIONS = Path(__file__).resolve().parents[1] / "database" / "pgsql" / "migrations"


@pytest.fixture(scope="module")
def gate():
    spec = importlib.util.spec_from_file_location("check_deployed_schema", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_deployed_schema"] = module
    spec.loader.exec_module(module)
    return module


class TestComparison:
    def test_in_sync_is_clean(self, gate):
        assert gate.missing_versions(["1", "2"], ["1", "2"]) == []

    def test_a_behind_database_is_reported(self, gate):
        """The 0.8.31 shape: the checkout has a migration production does not."""
        assert gate.missing_versions(["1", "2", "3"], ["1", "2"]) == ["3"]

    def test_an_ahead_database_does_not_block(self, gate):
        """Normal during a rollback — the deployed schema keeps the newer
        migration while the code goes back. Blocking here would make rolling
        back impossible exactly when it is most needed."""
        assert gate.missing_versions(["1", "2"], ["1", "2", "3"]) == []

    def test_every_gap_is_listed_not_just_the_first(self, gate):
        """Fixing one and rediscovering the next on the following release is the
        slow version of this bug."""
        assert gate.missing_versions(["1", "2", "3", "4"], ["1"]) == ["2", "3", "4"]

    def test_order_of_the_applied_list_does_not_matter(self, gate):
        assert gate.missing_versions(["1", "2"], ["2", "1"]) == []


class TestVersionParsing:
    def test_the_numeric_prefix_is_the_version(self, gate, tmp_path):
        (tmp_path / "20260816120000_add_original_spec.sql").touch()
        (tmp_path / "20260725120000_add_job_difficulty.sql").touch()

        assert gate.versions_in(tmp_path) == ["20260725120000", "20260816120000"]

    def test_a_short_version_is_still_read(self, gate, tmp_path):
        """20260304_rename_trello_card_id.sql is real and has no time component."""
        (tmp_path / "20260304_rename_trello_card_id.sql").touch()

        assert gate.versions_in(tmp_path) == ["20260304"]

    def test_non_sql_files_are_ignored(self, gate, tmp_path):
        (tmp_path / "20260816120000_real.sql").touch()
        (tmp_path / "README.md").touch()

        assert gate.versions_in(tmp_path) == ["20260816120000"]

    def test_it_reads_the_real_migrations_directory(self, gate):
        """Guard the glob: a parser that silently matches nothing would report
        'no migrations missing' forever, which is the failure this gate exists
        to prevent, arriving through the gate itself."""
        found = gate.local_versions()

        assert len(found) >= 12, f"expected the real migrations, found {found}"
        assert "20260816120000" in found, "the migration whose absence caused the incident"


class TestTheIncidentItCatches:
    def test_0_8_31_would_have_been_blocked(self, gate):
        """Reconstruction. At the 0.8.31 release the checkout had
        20260816120000 and production's latest was 20260725220000.
        """
        checkout = gate.versions_in(MIGRATIONS)
        production_then = [v for v in checkout if v < "20260816120000"]

        missing = gate.missing_versions(checkout, production_then)

        assert missing == ["20260816120000"]
        assert missing, "the gate must refuse this release"
