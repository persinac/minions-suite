"""The scout finding contract: what a finding must carry to become a card.

Every refusal is tested with a near-miss beside it, so the contract cannot
quietly tighten (refusing good findings) or loosen (filing vague ones).
"""

import pytest

from minions.core.scout_contract import KINDS, ScoutFindingError, normalise_fingerprint, validate_finding

GOOD = {
    "kind": "hot_spot",
    "title": "routes.py swallows token parse errors and returns 200",
    "evidence": ["src/app/routes.py:142", "src/app/auth.py:30-44"],
    "scope": "Raise on a bad token in parse_token and map it to 401 in routes.py; leave the session code alone.",
    "oracle": "A new test calling GET /me with token='' gets 401; today it gets 200 with an empty body.",
    "fingerprint": "hot_spot:src/app/routes.py:parse_token",
}


def _with(**changes) -> dict:
    return {**GOOD, **changes}


def _remedy(**changes) -> str:
    with pytest.raises(ScoutFindingError) as err:
        validate_finding(**_with(**changes))
    return err.value.remedy


def test_a_well_formed_finding_passes():
    validate_finding(**GOOD)


def test_every_kind_is_accepted():
    for kind in KINDS:
        validate_finding(**_with(kind=kind))


def test_an_unknown_kind_is_refused_with_the_list():
    remedy = _remedy(kind="other")
    assert "hot_spot" in remedy


class TestEvidence:
    def test_no_evidence_is_an_opinion(self):
        assert "no evidence" in _remedy(evidence=[])

    def test_a_path_without_a_line_is_refused(self):
        assert "path:line" in _remedy(evidence=["src/app/routes.py"])

    def test_prose_is_refused(self):
        assert "path:line" in _remedy(evidence=["the routes file around line 140"])

    def test_a_line_range_is_accepted(self):
        validate_finding(**_with(evidence=["src/app/routes.py:140-160"]))


class TestOracle:
    @pytest.mark.parametrize("oracle", ["tests pass", "Tests pass.", "CI is green", "ci passes!", "It works"])
    def test_a_process_oracle_is_refused(self, oracle):
        assert "process, not the outcome" in _remedy(oracle=oracle)

    def test_an_empty_oracle_is_refused(self):
        assert "no oracle" in _remedy(oracle="   ")

    def test_a_too_short_oracle_is_refused(self):
        assert "too short" in _remedy(oracle="run pytest")

    def test_an_outcome_oracle_that_mentions_tests_is_fine(self):
        """'tests pass' as a phrase inside a real check is not the process oracle."""
        validate_finding(**_with(oracle="tests pass after adding test_parse_token_empty, which fails on main today"))


class TestOtherFields:
    def test_empty_title_is_refused(self):
        assert "no title" in _remedy(title="  ")

    def test_long_title_is_refused(self):
        assert "under 120" in _remedy(title="x" * 121)

    def test_empty_scope_is_refused(self):
        assert "no scope" in _remedy(scope="")

    @pytest.mark.parametrize("fingerprint", ["", "abc", "Has Spaces:in it", "-leading-dash"])
    def test_a_malformed_fingerprint_is_refused(self, fingerprint):
        assert "fingerprint" in _remedy(fingerprint=fingerprint)


def test_fingerprints_are_scoped_to_their_repo():
    assert normalise_fingerprint("Wallet-API", " Hot_Spot:src/x.py ") == "wallet-api:hot_spot:src/x.py"
    assert normalise_fingerprint("a", "f:p") != normalise_fingerprint("b", "f:p")
