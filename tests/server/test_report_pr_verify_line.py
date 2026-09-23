"""`report_pr` must refuse a PR body that carries no usable VERIFY: line.

The engineer prompt has asked for that line since it was written. Nothing read
the body back, so the convention gated nothing: an engineer could omit it, or
write "CI is green", and the pipeline advanced exactly as if it had named a real
measurement. A prompt instruction is only as real as the tool that reads the
artifact back -- `service.test_command` is the standing example, interpolated
into the same prompt and gating nothing since it shipped.

Two asymmetries are load-bearing and both are tested here:

* **Unreadable body -> ALLOW.** A transient GitHub error must not fail a job
  whose work is genuinely finished. Same rule `_verify_reported_pr` already
  follows, for the same reason.
* **Readable body with no line -> REFUSE, retryably.** The agent is handed the
  remedy verbatim and edits the PR description. Nothing is recorded first, so
  the task sits exactly where it was.

The `null` case is the one that looks like a detail and is not: jq renders a PR
opened with an empty description as the four characters `null`. That is the one
body in existence guaranteed to have no VERIFY: line, so treating it as a fetch
failure would let it through the gate written to catch exactly that.

What would make these wrong: if `_require_verify_line` stopped raising,
`TestRefusal` fails. If it started raising on transient errors, `TestFailsOpen`
fails. If the call were dropped from `report_pr`, `TestWiring` fails while every
other test in this file still passes -- which is why the wiring test exists.
"""

import inspect
import subprocess
from unittest.mock import patch

import pytest

from minions.core.pr_contract import PRVerificationError
from minions.server.mcp import _require_verify_line

URL = "https://github.com/persinac/minions-suite/pull/106"

GOOD_BODY = "## Summary\n\nDoes the thing.\n\nVERIFY: `pytest tests/core/test_pr_contract.py` collects 27, not 0.\n"
NO_LINE_BODY = "## Summary\n\nDoes the thing. Trust me.\n"


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class TestRefusal:
    @pytest.mark.asyncio
    async def test_a_body_with_no_verify_line_is_refused(self):
        with patch("subprocess.run", return_value=_completed(0, stdout=NO_LINE_BODY)):
            with pytest.raises(PRVerificationError) as exc:
                await _require_verify_line(URL, 106)

        assert "does not contain a `VERIFY:` line" in exc.value.remedy
        assert "prompts/agents/engineer.md" in exc.value.remedy

    @pytest.mark.asyncio
    async def test_an_empty_pr_description_is_refused(self):
        """jq prints `null` for a PR opened with no description. It is empty,
        not unreadable -- the one body guaranteed to lack the line."""
        with patch("subprocess.run", return_value=_completed(0, stdout="null\n")):
            with pytest.raises(PRVerificationError):
                await _require_verify_line(URL, 106)

    @pytest.mark.asyncio
    async def test_a_process_claim_is_refused(self):
        with patch("subprocess.run", return_value=_completed(0, stdout="## Summary\n\nstuff\n\nVERIFY: CI is green.\n")):
            with pytest.raises(PRVerificationError) as exc:
                await _require_verify_line(URL, 106)

        assert "process" in exc.value.remedy


class TestAcceptance:
    @pytest.mark.asyncio
    async def test_a_body_with_a_real_measurement_passes(self):
        with patch("subprocess.run", return_value=_completed(0, stdout=GOOD_BODY)):
            await _require_verify_line(URL, 106)

    @pytest.mark.asyncio
    async def test_it_reads_the_body_of_the_pr_it_was_given(self):
        """A gate that asked GitHub about the wrong PR would pass or fail for
        reasons unrelated to this change."""
        with patch("subprocess.run", return_value=_completed(0, stdout=GOOD_BODY)) as run:
            await _require_verify_line(URL, 106)

        argv = " ".join(run.call_args[0][0])
        assert "repos/persinac/minions-suite/pulls/106" in argv
        assert ".body" in argv


class TestFailsOpen:
    """Absence of proof is not proof of absence. Failing a job whose work is
    genuinely finished is the worse error, and the merge gate downstream still
    refuses an unmergeable PR."""

    @pytest.mark.asyncio
    async def test_a_transient_error_does_not_refuse(self):
        with patch("subprocess.run", return_value=_completed(1, stderr="dial tcp: connection reset")):
            await _require_verify_line(URL, 106)

    @pytest.mark.asyncio
    async def test_a_missing_gh_binary_does_not_refuse(self):
        with patch("subprocess.run", side_effect=OSError("gh not found")):
            await _require_verify_line(URL, 106)

    @pytest.mark.asyncio
    async def test_a_timeout_does_not_refuse(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="gh", timeout=30)):
            await _require_verify_line(URL, 106)

    @pytest.mark.asyncio
    async def test_a_gitlab_mr_is_not_second_guessed(self):
        """The fetch is `gh`. This gate reaches GitHub PRs and says so rather
        than inventing a verdict for a provider it cannot read."""
        with patch("subprocess.run", side_effect=AssertionError("must not shell out for a GitLab MR")):
            await _require_verify_line("https://gitlab.com/group/proj/-/merge_requests/4", 4)


class TestWiring:
    """The helper being correct is worth nothing if `report_pr` never calls it.

    This is the test that fails if the gate is dropped; every other test in this
    file would keep passing.
    """

    def _report_pr_body(self) -> str:
        from minions.server import mcp as mcp_module

        source = inspect.getsource(mcp_module)
        start = source.index("async def report_pr")
        # Bounded by the next tool registration, not a character count. A window
        # that silently stops covering the thing it asserts on reads green while
        # the call is absent.
        end = source.find("@mcp.tool()", start)
        if end == -1:
            end = len(source)
        return source[start:end]

    def test_report_pr_checks_the_verify_line(self):
        assert "_require_verify_line(pr_url, pr_number)" in self._report_pr_body()

    def test_the_check_precedes_any_recording(self):
        """A body refused after the task moved to PR_OPEN would leave the task
        advanced on work the gate rejected."""
        body = self._report_pr_body()
        gate = body.index("_require_verify_line")

        assert gate < body.index("_propose_transition"), "the VERIFY: check must precede the PR_OPEN transition"
        assert gate < body.index("db.update_task("), "the VERIFY: check must precede any task update"

    def test_the_refusal_is_returned_as_retryable(self):
        """Handled the way submit_refined_spec handles SpecContractError: the
        remedy verbatim, marked retryable, so the agent edits the body and calls
        again instead of treating a fixable refusal as failure."""
        body = self._report_pr_body()

        assert "except PRVerificationError as e:" in body
        assert '"error": e.remedy, "retryable": True' in body

    def test_the_existing_pr_existence_check_still_runs_first(self):
        """Asking GitHub for the body of a PR that does not exist would report
        an unreadable body and fail open, quietly skipping this gate."""
        body = self._report_pr_body()

        assert body.index("_verify_reported_pr") < body.index("_require_verify_line")
