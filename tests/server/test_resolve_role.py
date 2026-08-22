"""Role-name resolution for create_task — pinning the arbiter's actual spellings.

The arbiter invents engineer names from context: 'software_engineer' (job
770264bf) and 'python_engineer' (job 1ddb3283) on back-to-back queue runs,
each costing a turn to the ValueError before the catch-all existed. The
prompt now lists the valid roles too, but a prompt is advice — this is the
enforcement, and it must map every engineer-shaped spelling somewhere sane
rather than teach the arbiter role names one refusal at a time.
"""

import pytest

from minions.core.models import AgentRole
from minions.server.mcp import _resolve_role


class TestExactRoles:
    @pytest.mark.parametrize("role", list(AgentRole))
    def test_every_canonical_role_resolves_to_itself(self, role):
        assert _resolve_role(role.value) is role

    def test_dashes_normalize(self):
        assert _resolve_role("backend-engineer") is AgentRole.BACKEND_ENGINEER


class TestHallucinatedEngineers:
    @pytest.mark.parametrize(
        "raw",
        [
            "software_engineer",  # job 770264bf
            "python_engineer",  # job 1ddb3283
            "engineer",
            "developer",
            "dev",
            "senior_engineer",
        ],
    )
    def test_engineer_shaped_names_default_to_backend(self, raw):
        assert _resolve_role(raw) is AgentRole.BACKEND_ENGINEER

    def test_database_engineer_spellings_still_win_over_the_catch_all(self):
        """Ordering: the specific checks run first, so a database-flavoured
        engineer never falls through to backend."""
        assert _resolve_role("database_migration_engineer") is AgentRole.DATABASE_ENGINEER

    def test_frontend_engineer_spellings_still_win_over_the_catch_all(self):
        assert _resolve_role("frontend_ui_engineer") is AgentRole.FRONTEND_ENGINEER


class TestStillRefusesNonRoles:
    @pytest.mark.parametrize("raw", ["", "banana", "manager", "qa"])
    def test_a_name_with_no_role_signal_raises_with_the_valid_list(self, raw):
        with pytest.raises(ValueError, match="Valid roles"):
            _resolve_role(raw)
