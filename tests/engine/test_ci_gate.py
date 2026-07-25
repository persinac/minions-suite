"""The CI gate on auto-merge. Fails closed.

Auto-merge went live across 33 repos with nothing between the reviewer's verdict
and merge_mr. renovate's should_auto_merge is NOT a usable reference: it treats
an empty ci_status as success (the no-CI free pass), it reads the legacy combined
commit status which GitHub Actions check-runs do not populate, and
get_pipeline_status — the method it depends on — is defined nowhere, so that gate
has never executed.

Design per the ui-integration-tests agent: read required check names from branch
protection at RUNTIME so the gate cannot drift from what GitHub enforces, resolve
them against check-runs on the PR head, and treat absent/empty as BLOCK.
"""

import pytest

from minions.config import Config
from minions.engine.dev import _ci_gate_passes


class _Provider:
    """Stands in for GitHubProvider with the three gate methods."""

    def __init__(self, required=None, runs=None, sha="abc123def456", explode=None):
        self._required = required if required is not None else []
        self._runs = runs or {}
        self._sha = sha
        self._explode = explode

    async def get_required_checks(self, project_id, branch):
        if self._explode == "protection":
            raise RuntimeError("HTTP 500")
        return self._required

    async def get_pr_head_sha(self, project_id, mr_id):
        return self._sha

    async def get_check_runs(self, project_id, sha):
        if self._explode == "checks":
            raise RuntimeError("HTTP 500")
        return self._runs


class _Project:
    project_id = "flippin-balls/wallet-api"


def _engine(require_ci_pass=True):
    config = Config.from_env()
    config.require_ci_pass = require_ci_pass
    return type("E", (), {"config": config})()


class TestFailsClosed:
    async def test_no_required_checks_blocks(self):
        """An ungated repo must not get a free pass — the opposite of renovate."""
        ok, reason = await _ci_gate_passes(_engine(), _Project(), _Provider(required=[]), "23", "main")

        assert ok is False
        assert "no required status checks" in reason

    async def test_required_check_that_never_reported_blocks(self):
        """Branch protection requires it; nothing posted it. That is not a pass."""
        provider = _Provider(required=["secret-scan", "lint"], runs={"secret-scan": "success"})

        ok, reason = await _ci_gate_passes(_engine(), _Project(), provider, "23", "main")

        assert ok is False
        assert "never reported" in reason
        assert "lint" in reason

    async def test_unreadable_branch_protection_blocks(self):
        ok, reason = await _ci_gate_passes(_engine(), _Project(), _Provider(explode="protection"), "23", "main")

        assert ok is False
        assert "branch protection" in reason

    async def test_unreadable_check_runs_blocks(self):
        provider = _Provider(required=["secret-scan"], explode="checks")

        ok, reason = await _ci_gate_passes(_engine(), _Project(), provider, "23", "main")

        assert ok is False
        assert "check-runs" in reason

    async def test_a_provider_without_the_methods_blocks(self):
        """GitLab, or an older provider, must block rather than silently pass."""

        class _Bare:
            pass

        ok, reason = await _ci_gate_passes(_engine(), _Project(), _Bare(), "23", "main")

        assert ok is False
        assert "cannot evaluate CI" in reason


class TestFailureConclusions:
    @pytest.mark.parametrize("conclusion", ["failure", "timed_out", "cancelled", "action_required", "stale"])
    async def test_non_green_conclusions_block(self, conclusion):
        provider = _Provider(required=["secret-scan"], runs={"secret-scan": conclusion})

        ok, reason = await _ci_gate_passes(_engine(), _Project(), provider, "23", "main")

        assert ok is False, f"{conclusion} must not merge"
        assert "not green" in reason

    @pytest.mark.parametrize("status", ["in_progress", "queued", "pending"])
    async def test_still_running_blocks(self, status):
        """A check that has not finished is not a passing check."""
        provider = _Provider(required=["test"], runs={"test": status})

        ok, _ = await _ci_gate_passes(_engine(), _Project(), provider, "23", "main")

        assert ok is False


class TestPasses:
    async def test_all_green_passes(self):
        provider = _Provider(required=["secret-scan", "lint", "test"],
                             runs={"secret-scan": "success", "lint": "success", "test": "success"})

        ok, reason = await _ci_gate_passes(_engine(), _Project(), provider, "23", "main")

        assert ok is True
        assert "3 required checks green" in reason

    @pytest.mark.parametrize("conclusion", ["neutral", "skipped"])
    async def test_neutral_and_skipped_count_as_green(self, conclusion):
        """A skipped job (path filter didn't match) is not a failure."""
        provider = _Provider(required=["lint"], runs={"lint": conclusion})

        ok, _ = await _ci_gate_passes(_engine(), _Project(), provider, "23", "main")

        assert ok is True

    async def test_extra_unrequired_failures_do_not_block(self):
        """Only branch protection decides what is required."""
        provider = _Provider(required=["secret-scan"],
                             runs={"secret-scan": "success", "some-optional-job": "failure"})

        ok, _ = await _ci_gate_passes(_engine(), _Project(), provider, "23", "main")

        assert ok is True

    async def test_the_gate_can_be_disabled(self):
        ok, reason = await _ci_gate_passes(_engine(require_ci_pass=False), _Project(), _Provider(), "23", "main")

        assert ok is True
        assert "disabled" in reason


class TestWiring:
    def test_the_gate_runs_before_merge(self):
        """A gate evaluated after merge_mr protects nothing."""
        import inspect

        from minions.engine import dev

        source = inspect.getsource(dev.run_task_review)
        gate = source.index("_ci_gate_passes")
        merge = source.index("merge_provider.merge_mr")

        assert gate < merge, "the CI gate must precede the merge call"

    def test_check_runs_not_combined_status(self):
        """Actions post check-runs; /commits/{sha}/status does not aggregate them."""
        import inspect

        from minions.providers.git import GitHubProvider

        source = inspect.getsource(GitHubProvider.get_check_runs)
        # Strip the docstring: it deliberately mentions /status to explain why
        # that endpoint is wrong, so a naive substring check trips on the comment.
        body = source.split('"""')[-1]

        assert 'commits/{sha}/check-runs' in body
        assert 'commits/{sha}/status' not in body, "must not call the combined status endpoint"
