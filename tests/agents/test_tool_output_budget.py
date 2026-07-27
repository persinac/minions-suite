"""Tool results are re-sent every turn, so their size is a recurring cost.

94% of agent spend is input, because the whole prefix goes back over the wire
each turn. A tool result is therefore not paid once — it is paid for every turn
that follows it. `_run_command` allowed 20,000 chars of stdout, which on a
suite like management-api's 1367 tests is both enormous and, worse, the wrong
20,000.

The old truncation was `[:20_000]` — head-only. pytest puts the failing
assertion partway down its output and the line that actually answers "did it
pass" at the very bottom:

    ===== 5 failed, 1362 passed in 41.20s =====

Cutting the tail threw that away, so the agent could not tell success from
failure and ran the suite again to find out: a second full-cost turn to recover
information the first one already contained. Keeping both ends is the point;
the smaller budget is secondary.

Read accounting is measurement only. Serving a stub for a repeat read is a
behaviour change — an agent denied content it believes it needs can loop in
ways that cost more than the duplicate — so the numbers come first.
"""

from unittest.mock import MagicMock

import pytest

from minions.agents.tools.mcp_executor import _STDERR_BUDGET, _STDOUT_BUDGET, McpToolExecutor, _elide


class TestElision:
    def test_short_text_is_untouched(self):
        assert _elide("hello", 4_000) == "hello"

    def test_text_exactly_at_budget_is_untouched(self):
        text = "x" * 4_000
        assert _elide(text, 4_000) == text

    def test_the_head_survives(self):
        text = "FIRST" + ("x" * 10_000)
        assert _elide(text, 4_000).startswith("FIRST")

    def test_the_tail_survives(self):
        """The whole reason this function exists."""
        text = ("x" * 10_000) + "5 failed, 1362 passed"
        assert _elide(text, 4_000).endswith("5 failed, 1362 passed")

    def test_it_says_how_much_it_dropped(self):
        """Silent truncation reads as complete output."""
        out = _elide("x" * 10_000, 4_000)
        assert "elided" in out
        assert "6000 chars elided" in out

    def test_output_stays_near_the_budget(self):
        """The marker adds a little; it must not add a lot."""
        out = _elide("x" * 100_000, 4_000)
        assert len(out) < 4_000 + 200

    def test_head_is_larger_than_tail(self):
        """Errors cluster early; the tail only needs the verdict line."""
        text = "H" * 20_000
        out = _elide(text, 3_000)
        head, tail = out.split("\n... [")[0], out.split("] ...\n")[1]
        assert len(head) > len(tail)


class TestBudgets:
    def test_stdout_budget_is_far_below_the_old_cap(self):
        assert _STDOUT_BUDGET <= 4_000, "the 20k cap is what made command output dominate the prefix"

    def test_stderr_gets_less_than_stdout(self):
        assert _STDERR_BUDGET < _STDOUT_BUDGET


def _executor(tmp_path) -> McpToolExecutor:
    return McpToolExecutor(
        mcp_server=MagicMock(),
        job_id="j1",
        task_id="t1",
        agent_id="a1",
        agent_role="backend_engineer",
        working_dir=str(tmp_path),
    )


class TestReadAccounting:
    async def test_a_single_read_is_not_flagged(self, tmp_path):
        (tmp_path / "f.py").write_text("print(1)\n")
        ex = _executor(tmp_path)

        await ex._read_file({"path": "f.py"})

        assert ex.read_stats()["rereads"] == 0

    async def test_a_repeat_read_is_counted(self, tmp_path):
        (tmp_path / "f.py").write_text("print(1)\n")
        ex = _executor(tmp_path)

        await ex._read_file({"path": "f.py"})
        await ex._read_file({"path": "f.py"})

        stats = ex.read_stats()
        assert stats["rereads"] == 1
        assert stats["total_reads"] == 2
        assert stats["files_read"] == 1

    async def test_the_content_is_still_returned(self, tmp_path):
        """Measurement must not change behaviour — that decision comes later,
        from the numbers this produces."""
        (tmp_path / "f.py").write_text("print(1)\n")
        ex = _executor(tmp_path)

        first = await ex._read_file({"path": "f.py"})
        second = await ex._read_file({"path": "f.py"})

        assert first == second == "print(1)"

    async def test_a_different_line_range_is_not_a_reread(self, tmp_path):
        """Reading 3-4 after 1-2 is genuinely new content; counting it as waste
        would overstate the problem and discredit the measurement."""
        (tmp_path / "f.py").write_text("a\nb\nc\nd\n")
        ex = _executor(tmp_path)

        await ex._read_file({"path": "f.py", "start_line": 1, "end_line": 2})
        await ex._read_file({"path": "f.py", "start_line": 3, "end_line": 4})

        assert ex.read_stats()["rereads"] == 0

    async def test_wasted_chars_accumulate(self, tmp_path):
        (tmp_path / "f.py").write_text("x" * 100)
        ex = _executor(tmp_path)

        for _ in range(3):
            await ex._read_file({"path": "f.py"})

        stats = ex.read_stats()
        assert stats["rereads"] == 2
        assert stats["reread_chars"] == 200

    async def test_worst_offenders_are_ranked(self, tmp_path):
        (tmp_path / "hot.py").write_text("h\n")
        (tmp_path / "cold.py").write_text("c\n")
        ex = _executor(tmp_path)

        for _ in range(4):
            await ex._read_file({"path": "hot.py"})
        for _ in range(2):
            await ex._read_file({"path": "cold.py"})

        worst = ex.read_stats()["worst"]
        assert worst[0][0].startswith("hot.py")
        assert worst[0][1] == 4


class TestRunnerReporting:
    def test_it_tolerates_an_executor_without_read_stats(self):
        """Reviewers use a different executor with no filesystem tools."""
        from minions.agents.runner import _log_read_stats

        _log_read_stats(MagicMock(id="a1"), object())

    def test_it_tolerates_no_executor_at_all(self):
        from minions.agents.runner import _log_read_stats

        _log_read_stats(MagicMock(id="a1"), None)

    def test_a_failing_stats_call_does_not_break_completion(self):
        """This runs on the success path — it must never be what fails a run
        that otherwise worked."""
        from minions.agents.runner import _log_read_stats

        broken = MagicMock()
        broken.read_stats.side_effect = RuntimeError("boom")

        _log_read_stats(MagicMock(id="a1"), broken)


class TestSpecAnalystPlanGranularity:
    """The arbiter was already coarse; the spec analyst runs FIRST and was not."""

    @pytest.fixture(scope="class")
    def prompt(self) -> str:
        from pathlib import Path

        return (Path(__file__).resolve().parents[2] / "prompts" / "agents" / "spec_analyst.md").read_text()

    def test_one_task_per_service_is_the_default(self, prompt):
        assert "One task per service is the" in prompt

    def test_it_explains_that_splitting_multiplies_cost(self, prompt):
        """Without the why, "fewest tasks" reads as a style preference."""
        assert "does not divide the cost, it multiplies" in prompt

    def test_implementation_and_tests_are_not_split(self, prompt):
        assert "Implementation vs. tests" in prompt

    def test_implementation_and_wiring_are_not_split(self, prompt):
        assert "integration/wiring" in prompt

    def test_role_and_repo_boundaries_are_still_respected(self, prompt):
        """An agent has one working tree and one toolchain — these splits are
        not waste, they are the only ones that are real."""
        assert "A different service or repo" in prompt
        assert "A different agent role" in prompt
        assert "database_engineer" in prompt

    def test_oversized_work_is_reported_not_shredded(self, prompt):
        assert "rather than shredding it" in prompt
