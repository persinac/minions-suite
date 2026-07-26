"""A self-reported PR must exist, and must belong to the branch claimed.

`report_pr` is how an agent tells the system it opened a PR. Everything
downstream — the specialist review fan-out, the CI merge gate, auto-merge —
treats that report as fact. The only check was `bool(task.pr_url)`: "is the
string non-empty".

Job 2e9cd9e3 reported pull/80 having created nothing; 80 was simply the next
free number. The task advanced to PR_OPEN and three specialist reviewers
launched against a PR that did not exist and billed for it. The system even had
proof — labelling returned "Could not resolve to a PullRequest with the number
80" — and logged it as a warning while the pipeline carried on.

Two properties are tested here:

* A confirmed-absent PR is REJECTED. Fails closed, because advancing on a
  fiction wastes reviewer spend and produces a job that can never merge.
* An inconclusive result is ALLOWED. A transient GitHub error must not fail a
  job whose work is genuinely finished — absence of proof is not proof of
  absence, and the merge gate downstream will still refuse an unmergeable PR.

The branch check is the safety-critical half. Existence alone still lets an
agent name somebody else's open PR, which with auto_merge on would review and
merge their work under this job's ticket. An agent cannot fake having pushed
the branch.
"""

import subprocess
from unittest.mock import patch

import pytest

from minions.server.mcp import _verify_reported_pr

URL = "https://github.com/flippin-balls/management-api/pull/80"


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class TestRejection:
    @pytest.mark.asyncio
    async def test_a_nonexistent_pr_is_rejected(self):
        """The exact failure from job 2e9cd9e3."""
        with patch("subprocess.run", return_value=_completed(1, stderr="gh: Not Found (HTTP 404)")):
            ok, why = await _verify_reported_pr(URL, 80, "feat/job-2e9cd9e3/x")

        assert ok is False
        assert "does not exist" in why

    @pytest.mark.asyncio
    async def test_the_graphql_wording_is_also_caught(self):
        """`gh pr edit` phrases it differently from `gh api`."""
        with patch("subprocess.run", return_value=_completed(1, stderr="GraphQL: Could not resolve to a PullRequest")):
            ok, _ = await _verify_reported_pr(URL, 80, "feat/x")

        assert ok is False

    @pytest.mark.asyncio
    async def test_a_pr_for_a_different_branch_is_rejected(self):
        """Existence alone would let an agent claim someone else's PR and,
        with auto_merge on, merge their work under this ticket."""
        with patch("subprocess.run", return_value=_completed(0, stdout="somebody-elses-branch\n")):
            ok, why = await _verify_reported_pr(URL, 42, "feat/job-2e9cd9e3/x")

        assert ok is False
        assert "head branch" in why

    @pytest.mark.asyncio
    async def test_no_pr_number_is_rejected(self):
        ok, _ = await _verify_reported_pr(URL, 0, "feat/x")

        assert ok is False


class TestAcceptance:
    @pytest.mark.asyncio
    async def test_a_real_pr_on_the_right_branch_is_accepted(self):
        with patch("subprocess.run", return_value=_completed(0, stdout="feat/job-2e9cd9e3/x\n")):
            ok, why = await _verify_reported_pr(URL, 80, "feat/job-2e9cd9e3/x")

        assert ok is True
        assert why == "ok"

    @pytest.mark.asyncio
    async def test_a_transient_error_does_not_fail_the_job(self):
        """Absence of proof is not proof of absence. The merge gate downstream
        still refuses an unmergeable PR, so allowing here is the safer error."""
        with patch("subprocess.run", return_value=_completed(1, stderr="dial tcp: connection reset")):
            ok, why = await _verify_reported_pr(URL, 80, "feat/x")

        assert ok is True
        assert "inconclusive" in why

    @pytest.mark.asyncio
    async def test_an_exception_does_not_fail_the_job(self):
        with patch("subprocess.run", side_effect=OSError("gh not found")):
            ok, _ = await _verify_reported_pr(URL, 80, "feat/x")

        assert ok is True

    @pytest.mark.asyncio
    async def test_a_non_github_url_is_not_second_guessed(self):
        """GitLab MRs go through a different path; do not invent a verdict."""
        ok, _ = await _verify_reported_pr("https://gitlab.com/x/y/-/merge_requests/3", 3, "feat/x")

        assert ok is True

    @pytest.mark.asyncio
    async def test_an_empty_branch_claim_skips_the_branch_check(self):
        """Existence still verified; only the comparison is skipped."""
        with patch("subprocess.run", return_value=_completed(0, stdout="whatever\n")):
            ok, _ = await _verify_reported_pr(URL, 80, "")

        assert ok is True


class TestWiring:
    def test_report_pr_verifies_before_transitioning(self):
        import inspect

        from minions.server import mcp as mcp_module

        source = inspect.getsource(mcp_module)
        start = source.index("async def report_pr")
        body = source[start : start + 2500]

        assert "_verify_reported_pr(pr_url, pr_number, branch_name)" in body
        gate = body.index("_verify_reported_pr")
        transition = body.index("_propose_transition")
        assert gate < transition, "verification must precede the PR_OPEN transition"
