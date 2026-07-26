"""The CI gate on auto-merge.

Two layers, and the second is not ours:

1. The repo must HAVE required checks. GitHub enforces nothing on an unprotected
   branch, so this is the only thing between an agent and an ungated repo. Fails
   closed — the inverse of renovate's should_auto_merge, where an empty
   ci_status counted as success.
2. Whether those checks are green is GitHub's call, read via mergeable_state and
   ultimately enforced by branch protection refusing the merge server-side, even
   for the App that opened the PR.

The gate deliberately no longer reads check-runs. That needs a Checks:read grant
the App does not have, and adding it requires the org to accept the permission —
friction for a judgement GitHub already makes, and a duplicated judgement is one
that can drift. mergeable_state comes with Pull requests:read and folds required
checks, required reviews and conflicts into one value.
"""

import json

import pytest

from minions.config import Config
from minions.engine.dev import _ci_gate_passes


class _Provider:
    def __init__(self, required=None, state="clean", explode=None):
        self._required = required if required is not None else []
        self._state = state
        self._explode = explode

    async def get_required_checks(self, project_id, branch):
        if self._explode == "rules":
            raise RuntimeError("HTTP 500")
        return self._required

    async def get_merge_state(self, project_id, mr_id):
        if self._explode == "state":
            raise RuntimeError("HTTP 500")
        return self._state


class _Project:
    project_id = "flippin-balls/wallet-api"


def _engine(require_ci_pass=True):
    config = Config.from_env()
    config.require_ci_pass = require_ci_pass
    return type("E", (), {"config": config})()


async def _gate(**kwargs):
    require_ci_pass = kwargs.pop("require_ci_pass", True)
    return await _ci_gate_passes(_engine(require_ci_pass), _Project(), _Provider(**kwargs), "23", "main")


class TestUngatedReposBlock:
    """The half GitHub will not do for us."""

    async def test_no_required_checks_blocks(self):
        ok, reason = await _gate(required=[])

        assert ok is False
        assert "no required status checks" in reason

    async def test_unreadable_branch_rules_blocks(self):
        ok, reason = await _gate(required=["secret-scan"], explode="rules")

        assert ok is False
        assert "branch rules" in reason

    async def test_a_provider_without_the_methods_blocks(self):
        """GitLab, or an older provider, must block rather than silently pass."""

        class _Bare:
            pass

        ok, reason = await _ci_gate_passes(_engine(), _Project(), _Bare(), "23", "main")

        assert ok is False
        assert "cannot evaluate CI" in reason


class TestGitHubsVerdict:
    @pytest.mark.parametrize(
        "state,expect_phrase",
        [
            ("blocked", "required checks"),
            ("dirty", "conflicts"),
            ("behind", "base branch"),
            ("draft", "draft"),
        ],
    )
    async def test_blocking_states_block_with_a_specific_reason(self, state, expect_phrase):
        ok, reason = await _gate(required=["secret-scan"], state=state)

        assert ok is False
        assert state in reason
        assert expect_phrase in reason, "the reason must explain WHY, not just name the state"

    @pytest.mark.parametrize("state", ["clean", "has_hooks"])
    async def test_mergeable_states_pass(self, state):
        ok, reason = await _gate(required=["secret-scan"], state=state)

        assert ok is True
        assert state in reason

    async def test_unstable_passes_because_the_ruleset_permits_it(self):
        """`unstable` = a NON-required check failing. Branch protection allows the
        merge; blocking here would second-guess the ruleset."""
        ok, _ = await _gate(required=["secret-scan"], state="unstable")

        assert ok is True

    async def test_unknown_defers_rather_than_blocking(self):
        """mergeable_state is computed asynchronously, so `unknown` right after a
        push is a timing artefact. Branch protection still gates the real merge,
        so stranding the PR here would cost a rerun for nothing."""
        ok, reason = await _gate(required=["secret-scan"], state="unknown")

        assert ok is True
        assert "deferring to branch protection" in reason

    async def test_an_unreadable_merge_state_blocks(self):
        ok, reason = await _gate(required=["secret-scan"], explode="state")

        assert ok is False
        assert "merge state" in reason


class TestDisable:
    async def test_the_gate_can_be_turned_off(self):
        ok, reason = await _gate(required=[], require_ci_pass=False)

        assert ok is True
        assert "disabled" in reason


class TestWiring:
    def test_the_gate_runs_before_merge(self):
        """A gate evaluated after merge_mr protects nothing."""
        import inspect

        from minions.engine import dev

        source = inspect.getsource(dev.run_task_review)

        assert source.index("_ci_gate_passes") < source.index("merge_provider.merge_mr")

    def test_a_refused_merge_is_recorded_distinctly(self):
        """Branch protection refusing IS the gate — it must not read as a
        transient API failure in the audit trail."""
        import inspect

        from minions.engine import dev

        assert "auto_merge_refused" in inspect.getsource(dev.run_task_review)

    def test_check_runs_is_no_longer_a_dependency(self):
        """Dropping it is what avoids needing an org-accepted App permission."""
        import inspect

        from minions.engine import dev

        source = inspect.getsource(dev._ci_gate_passes)

        assert "get_check_runs" not in source
        assert "get_merge_state" in source


class TestRequiredChecksSource:
    """The gate must read the surface org rulesets actually appear on.

    It originally read /branches/{branch}/protection/required_status_checks —
    classic branch protection. An org ruleset does not appear there at all.
    Verified live against wallet-api@main: that endpoint returned "Resource not
    accessible by integration" while /rules/branches/main returned the org
    ruleset (id 19750440) with required_status_checks: ['secret-scan'].
    """

    @staticmethod
    def _provider(payload):
        from minions.providers.git import GitHubProvider

        p = GitHubProvider(token="t")
        p._run_gh = lambda args, timeout=30, stdin=None: payload
        return p

    async def test_reads_the_rules_endpoint_not_classic_protection(self):
        import inspect

        from minions.providers.git import GitHubProvider

        body = inspect.getsource(GitHubProvider.get_required_checks).split('"""')[-1]

        assert "rules/branches/" in body
        assert "protection/required_status_checks" not in body

    async def test_extracts_contexts_from_the_live_shape(self):
        payload = json.dumps(
            [
                {"type": "non_fast_forward", "ruleset_source_type": "Organization", "ruleset_id": 19750440},
                {"type": "deletion"},
                {"type": "pull_request", "parameters": {"dismiss_stale_reviews_on_push": True}},
                {"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": "secret-scan"}]}},
            ]
        )

        assert await self._provider(payload).get_required_checks("org/repo", "main") == ["secret-scan"]

    async def test_merges_contexts_across_rulesets(self):
        """An org ruleset and a repo ruleset can both target one branch."""
        payload = json.dumps(
            [
                {"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": "secret-scan"}]}},
                {"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": "lint"}, {"context": "secret-scan"}]}},
            ]
        )

        checks = await self._provider(payload).get_required_checks("org/repo", "main")

        assert sorted(checks) == ["lint", "secret-scan"]

    async def test_no_status_check_rule_returns_empty(self):
        payload = json.dumps([{"type": "non_fast_forward"}, {"type": "deletion"}])

        assert await self._provider(payload).get_required_checks("org/repo", "main") == []

    async def test_malformed_payloads_return_empty(self):
        for payload in ("not json", json.dumps({"message": "Not Found"}), "[]"):
            assert await self._provider(payload).get_required_checks("org/repo", "main") == []
