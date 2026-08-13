"""An agent that pushed a branch but lost the PR number must not lose the work.

Job 1d7b7374, the management-api e2e run:

    06:59:49  report_pr REJECTED (pr=0 branch=feat/job-1d7b7374/writerows-...)
    07:00:03  engineer done: $0.4134, 58 turns
    07:00:05  orphan recovery: has edits but no PR -- launching finisher
    07:00:32  finisher opens PR #89
    07:00:47  finisher "ended done without reporting a PR"
    07:00:50  job -> failed

Two agents in a row pushed real work, opened a real PR, and failed to report the
number. The rejection message even told them what to do. Meanwhile PR #89 sat
open with lint, test and secret-scan all green -- correct, tested work that the
pipeline had thrown away.

The branch is the one identifier an agent cannot fake, because it had to push
it. Resolving the PR from the branch replaces a self-reported number with an
observed fact, so this is stricter than trusting the agent, not looser. The
resolved number still goes through _verify_reported_pr.
"""

import json
from unittest.mock import patch

import pytest

from minions.server.mcp import _repo_from_url, _resolve_pr_by_branch


class _Result:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


class TestRepoFromUrl:
    def test_a_full_pr_url(self):
        assert _repo_from_url("https://github.com/flippin-balls/management-api/pull/89") == "flippin-balls/management-api"

    def test_a_url_with_no_pull_tail(self):
        """The missing tail is precisely the symptom being recovered from."""
        assert _repo_from_url("https://github.com/flippin-balls/management-api") == "flippin-balls/management-api"

    def test_a_git_suffix_is_stripped(self):
        assert _repo_from_url("https://github.com/flippin-balls/management-api.git") == "flippin-balls/management-api"

    def test_no_url_at_all(self):
        assert _repo_from_url("") == ""

    def test_a_non_github_url(self):
        assert _repo_from_url("https://gitlab.com/group/proj/-/merge_requests/4") == ""


class TestResolvePrByBranch:
    @pytest.mark.asyncio
    async def test_a_single_matching_open_pr_is_resolved(self):
        with patch("subprocess.run", return_value=_Result(stdout="[89]\n")):
            got = await _resolve_pr_by_branch("flippin-balls/management-api", "feat/job-1d7b7374/writerows-sanitizing-writer")

        assert got == 89

    @pytest.mark.asyncio
    async def test_no_match_resolves_to_nothing(self):
        with patch("subprocess.run", return_value=_Result(stdout="[]\n")):
            assert await _resolve_pr_by_branch("owner/repo", "some-branch") is None

    @pytest.mark.asyncio
    async def test_two_matches_refuse_to_guess(self):
        """Guessing would hand the pipeline somebody else's PR, and with
        auto_merge on it would merge their work under this job's ticket."""
        with patch("subprocess.run", return_value=_Result(stdout="[12, 34]\n")):
            assert await _resolve_pr_by_branch("owner/repo", "shared-branch") is None

    @pytest.mark.asyncio
    async def test_a_failed_lookup_resolves_to_nothing(self):
        with patch("subprocess.run", return_value=_Result(returncode=1, stderr="gh: not found")):
            assert await _resolve_pr_by_branch("owner/repo", "b") is None

    @pytest.mark.asyncio
    async def test_an_exception_resolves_to_nothing(self):
        with patch("subprocess.run", side_effect=OSError("gh missing")):
            assert await _resolve_pr_by_branch("owner/repo", "b") is None

    @pytest.mark.asyncio
    async def test_unparseable_output_resolves_to_nothing(self):
        with patch("subprocess.run", return_value=_Result(stdout="not json")):
            assert await _resolve_pr_by_branch("owner/repo", "b") is None

    @pytest.mark.asyncio
    async def test_missing_inputs_short_circuit_without_calling_gh(self):
        with patch("subprocess.run") as run:
            assert await _resolve_pr_by_branch("", "b") is None
            assert await _resolve_pr_by_branch("owner/repo", "") is None

        run.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_query_filters_on_open_prs_by_head_ref(self):
        """A closed PR for a recycled branch name must not be resurrected."""
        with patch("subprocess.run", return_value=_Result(stdout="[89]")) as run:
            await _resolve_pr_by_branch("owner/repo", "my-branch")

        argv = run.call_args[0][0]
        joined = " ".join(argv)

        assert "state=open" in joined
        assert "head.ref" in joined
        assert "my-branch" in joined


class TestReportPrRecoversRatherThanRejecting:
    """The call-site wiring: resolution must happen BEFORE the rejection."""

    def _body(self) -> str:
        """Slice to the next tool registration, not a fixed character count.

        A fixed window is how a sibling test in this suite ended up green
        against the bug it was written for: the comment block above the call
        was longer than the window, so the call was never in the text being
        asserted on.
        """
        import inspect

        from minions.server import mcp as mcp_module

        source = inspect.getsource(mcp_module)
        start = source.index("async def report_pr")
        end = source.find("@mcp.tool()", start)
        if end == -1:
            end = len(source)
        return source[start:end]

    def test_resolution_is_attempted_before_rejecting(self):
        body = self._body()

        assert "_resolve_pr_by_branch" in body, "report_pr rejects a missing number without trying to resolve it"
        assert body.index("_resolve_pr_by_branch") < body.index("report_pr REJECTED"), "resolution must run before the rejection path"

    def test_the_resolved_number_is_still_verified(self):
        """Resolution finds a candidate; it does not bypass the safety check
        that the PR exists and has the claimed head branch."""
        body = self._body()

        assert body.index("_resolve_pr_by_branch") < body.index("_verify_reported_pr"), "the resolved PR must still go through verification"

    def test_resolution_only_fires_when_the_number_is_missing(self):
        """A reported number stays authoritative and fully checked."""
        body = self._body()

        assert "if not pr_number and branch_name:" in body, "resolution is not gated on a missing PR number"


def test_the_recovered_payload_shape_is_unchanged():
    """Downstream reads pr_number as an int; resolution must not hand back a str."""
    with patch("subprocess.run", return_value=_Result(stdout='["89"]')):
        import asyncio

        got = asyncio.run(_resolve_pr_by_branch("owner/repo", "b"))

    assert got == 89
    assert isinstance(got, int)


def test_json_import_is_available_to_the_resolver():
    """The resolver parses gh output with json; a missing import would only
    surface at runtime on the recovery path, which is rarely exercised."""
    from minions.server import mcp as mcp_module

    assert hasattr(mcp_module, "json")
    assert json.loads("[1]") == [1]
