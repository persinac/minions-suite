"""Tests for Renovate classifier — pure logic, no I/O."""

from minions.models import RiskLevel
from minions.renovate_classifier import (
    _compare_versions,
    classify_risk,
    is_renovate_mr,
    parse_version_bump,
    should_auto_merge,
)

# -----------------------------------------------------------------------
# Detection
# -----------------------------------------------------------------------


class TestIsRenovateMr:
    def test_matching_author(self):
        assert is_renovate_mr("renovate[bot]", "main", ["renovate[bot]"], ["renovate/"])

    def test_matching_branch_prefix(self):
        assert is_renovate_mr("human", "renovate/foo-1.2.3", [], ["renovate/"])

    def test_no_match(self):
        assert not is_renovate_mr("human", "feat/bar", ["renovate[bot]"], ["renovate/"])

    def test_multiple_bot_names(self):
        assert is_renovate_mr("dependabot", "main", ["renovate[bot]", "dependabot"], [])

    def test_multiple_prefixes(self):
        assert is_renovate_mr("human", "deps/update-foo", [], ["renovate/", "deps/"])


# -----------------------------------------------------------------------
# Version comparison
# -----------------------------------------------------------------------


class TestCompareVersions:
    def test_patch_bump(self):
        assert _compare_versions("1.2.3", "1.2.4") == RiskLevel.PATCH

    def test_minor_bump(self):
        assert _compare_versions("1.2.3", "1.3.0") == RiskLevel.MINOR

    def test_major_bump(self):
        assert _compare_versions("1.2.3", "2.0.0") == RiskLevel.MAJOR

    def test_same_version_is_patch(self):
        assert _compare_versions("1.2.3", "1.2.3") == RiskLevel.PATCH

    def test_two_part_versions(self):
        assert _compare_versions("1.2", "1.3") == RiskLevel.MINOR

    def test_version_with_prefix(self):
        assert _compare_versions("v1.2.3", "v2.0.0") == RiskLevel.MAJOR

    def test_empty_version_unknown(self):
        assert _compare_versions("", "1.2.3") == RiskLevel.UNKNOWN

    def test_invalid_version_unknown(self):
        assert _compare_versions("abc", "1.2.3") == RiskLevel.UNKNOWN


# -----------------------------------------------------------------------
# Version bump parsing
# -----------------------------------------------------------------------


class TestParseVersionBump:
    def test_from_to_pattern(self):
        dep, frm, to, risk = parse_version_bump(
            "Update dependency foo from 1.2.3 to 1.3.0",
            "renovate/foo-1.3.0",
        )
        assert dep == "foo"
        assert frm == "1.2.3"
        assert to == "1.3.0"
        assert risk == RiskLevel.MINOR

    def test_to_only_pattern(self):
        dep, frm, to, risk = parse_version_bump(
            "Update dependency bar to v2.0.0",
            "renovate/bar-2.0.0",
        )
        assert dep == "bar"
        assert frm == ""
        assert to == "2.0.0"
        assert risk == RiskLevel.MINOR  # can't determine without from_ver

    def test_pin_pattern(self):
        dep, _frm, to, _risk = parse_version_bump(
            "Pin dependency baz to 1.0.0",
            "renovate/pin-baz",
        )
        assert dep == "baz"
        assert to == "1.0.0"

    def test_monorepo_suffix_removed(self):
        dep, _, _, _ = parse_version_bump(
            "Update foo monorepo to v1.2.3",
            "renovate/foo-1.2.3",
        )
        assert dep == "foo"

    def test_major_hint_from_branch(self):
        _dep, _frm, _to, risk = parse_version_bump(
            "chore(deps): some update",
            "renovate/major-foo",
        )
        assert risk == RiskLevel.MAJOR

    def test_dep_name_from_branch_fallback(self):
        dep, _, _, _ = parse_version_bump("Unknown title", "renovate/my-lib-1.2.3")
        assert dep == "my-lib"

    def test_chore_deps_title(self):
        dep, _frm, _to, risk = parse_version_bump(
            "chore(deps): update foo from 1.0.0 to 2.0.0",
            "renovate/foo-2.0.0",
        )
        assert dep == "foo"
        assert risk == RiskLevel.MAJOR

    def test_scoped_package(self):
        dep, _, to, _ = parse_version_bump(
            "Update dependency @scope/pkg to ^3.0.0",
            "renovate/scope-pkg",
        )
        assert dep == "@scope/pkg"
        assert to == "3.0.0"


# -----------------------------------------------------------------------
# Risk classification
# -----------------------------------------------------------------------


class TestClassifyRisk:
    def test_patch_auto_merge(self):
        assert classify_risk(RiskLevel.PATCH, "foo", [], True, False, False) == "auto_merge"

    def test_patch_no_auto_merge(self):
        assert classify_risk(RiskLevel.PATCH, "foo", [], False, False, False) == "human_review"

    def test_minor_auto_merge(self):
        assert classify_risk(RiskLevel.MINOR, "foo", [], False, True, False) == "auto_merge"

    def test_major_auto_merge(self):
        assert classify_risk(RiskLevel.MAJOR, "foo", [], False, False, True) == "auto_merge"

    def test_major_no_auto_merge(self):
        assert classify_risk(RiskLevel.MAJOR, "foo", [], True, True, False) == "human_review"

    def test_unknown_always_human_review(self):
        assert classify_risk(RiskLevel.UNKNOWN, "foo", [], True, True, True) == "human_review"

    def test_excluded_package(self):
        assert classify_risk(RiskLevel.PATCH, "dangerous-lib", ["dangerous"], True, True, True) == "human_review"

    def test_excluded_package_case_insensitive(self):
        assert classify_risk(RiskLevel.PATCH, "MyLib", ["mylib"], True, True, True) == "human_review"

    def test_excluded_package_partial_match(self):
        assert classify_risk(RiskLevel.PATCH, "@scope/core-lib", ["core-lib"], True, True, True) == "human_review"


# -----------------------------------------------------------------------
# Final merge decision
# -----------------------------------------------------------------------


class TestShouldAutoMerge:
    def test_auto_merge_ci_passed(self):
        merge, reason = should_auto_merge("success", False, "auto_merge", True)
        assert merge is True
        assert "CI passed" in reason

    def test_no_merge_when_conflicts(self):
        merge, reason = should_auto_merge("success", True, "auto_merge", True)
        assert merge is False
        assert "conflicts" in reason

    def test_no_merge_human_review(self):
        merge, _reason = should_auto_merge("success", False, "human_review", True)
        assert merge is False

    def test_ci_running_still_merges(self):
        merge, reason = should_auto_merge("running", False, "auto_merge", True)
        assert merge is True
        assert "pipeline succeeds" in reason

    def test_ci_pending_still_merges(self):
        merge, _reason = should_auto_merge("pending", False, "auto_merge", True)
        assert merge is True

    def test_ci_failed_blocks_merge(self):
        merge, reason = should_auto_merge("failed", False, "auto_merge", True)
        assert merge is False
        assert "failed" in reason

    def test_ci_not_required(self):
        merge, _reason = should_auto_merge("", False, "auto_merge", False)
        assert merge is True

    def test_skip_decision(self):
        merge, _reason = should_auto_merge("success", False, "skip", True)
        assert merge is False
