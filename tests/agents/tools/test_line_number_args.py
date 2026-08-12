"""A line number typed by a model may arrive as a string, and read_file must survive it.

Observed live on job 68576a15 (playfield-relay), whose Trello card cited
`ActiveState.cpp:507`. The model passed `start_line="507"` and both copies of
`read_file` raised:

    start = max(1, start) - 1
    TypeError: '>' not supported between instances of 'str' and 'int'

The schema declares `{"type": "integer"}`, but nothing between the model and the
tool enforces it. The damage is quiet: `execute()` catches the exception and
hands back an error string, so the agent proceeds having read less of the file
than it asked for, with no signal that the *range* was the problem. The engineer
lost the read and retried blind; the reviewers then hit the same line twice more
from the second copy in definitions.py.

Two copies of the same code is why this is a shared helper and why both call
sites are tested here -- fixing one and not the other is exactly what happened
in production.
"""

import json
from pathlib import Path

import pytest

from minions.agents.tools.args import coerce_line_number
from minions.agents.tools.definitions import ToolExecutor
from minions.agents.tools.mcp_executor import McpToolExecutor


class TestCoerceLineNumber:
    def test_an_int_passes_through(self):
        assert coerce_line_number(507, "start_line") == 507

    def test_the_string_a_model_emits_is_accepted(self):
        """The actual production failure: a quoted integer."""
        assert coerce_line_number("507", "start_line") == 507

    def test_surrounding_whitespace_is_tolerated(self):
        assert coerce_line_number(" 507 ", "start_line") == 507

    def test_absent_stays_absent(self):
        """None means "no range requested", which is not the same as line 0."""
        assert coerce_line_number(None, "start_line") is None

    def test_an_empty_string_reads_as_absent_rather_than_an_error(self):
        """Models emit "" for "I don't want to constrain this"."""
        assert coerce_line_number("", "start_line") is None

    def test_a_whole_float_is_accepted(self):
        """JSON has one number type, so 507 can deserialize as 507.0."""
        assert coerce_line_number(507.0, "start_line") == 507

    def test_a_fractional_float_is_rejected(self):
        with pytest.raises(ValueError):
            coerce_line_number(507.5, "start_line")

    def test_a_bool_is_rejected_rather_than_silently_becoming_line_one(self):
        """bool subclasses int, so an unguarded isinstance(x, int) accepts True."""
        with pytest.raises(ValueError):
            coerce_line_number(True, "start_line")

    def test_a_non_numeric_string_is_rejected(self):
        with pytest.raises(ValueError):
            coerce_line_number("ActiveState.cpp:507", "start_line")

    def test_the_error_names_the_field_and_the_value(self):
        """The message is returned to the model as the tool result, so it has to
        carry enough for the agent to correct itself on the next turn."""
        with pytest.raises(ValueError) as excinfo:
            coerce_line_number("abc", "start_line")

        assert "start_line" in str(excinfo.value)
        assert "abc" in str(excinfo.value)


@pytest.fixture
def sample_file(tmp_path):
    """Ten numbered lines, so a returned slice identifies itself."""
    path = tmp_path / "ActiveState.cpp"
    path.write_text("\n".join(f"line{n}" for n in range(1, 11)), encoding="utf-8")
    return path


class TestReviewerReadFile:
    """definitions.ToolExecutor -- the copy the reviewers hit."""

    def _executor(self, tmp_path):
        return ToolExecutor(provider=None, project_id="p", mr_id="1", repo_path=str(tmp_path))

    def test_a_string_range_returns_the_lines_not_an_error(self, tmp_path, sample_file):
        out = self._executor(tmp_path)._read_file({"path": "ActiveState.cpp", "start_line": "3", "end_line": "5"})

        assert out == "line3\nline4\nline5"

    def test_a_string_range_matches_the_int_range(self, tmp_path, sample_file):
        """Same request, two spellings, one answer."""
        ex = self._executor(tmp_path)

        as_str = ex._read_file({"path": "ActiveState.cpp", "start_line": "3", "end_line": "5"})
        as_int = ex._read_file({"path": "ActiveState.cpp", "start_line": 3, "end_line": 5})

        assert as_str == as_int

    def test_a_string_start_alone_runs_to_the_end(self, tmp_path, sample_file):
        out = self._executor(tmp_path)._read_file({"path": "ActiveState.cpp", "start_line": "9"})

        assert out == "line9\nline10"

    def test_an_unparseable_range_explains_itself_instead_of_raising(self, tmp_path, sample_file):
        """It must reach the model as a description of the problem, not as the
        bare TypeError text that told the agent nothing about the range."""
        out = self._executor(tmp_path)._read_file({"path": "ActiveState.cpp", "start_line": "not-a-line"})

        assert "start_line" in json.loads(out)["error"]

    def test_no_range_still_returns_the_whole_file(self, tmp_path, sample_file):
        out = self._executor(tmp_path)._read_file({"path": "ActiveState.cpp"})

        assert out.splitlines() == [f"line{n}" for n in range(1, 11)]


class TestEngineerReadFile:
    """mcp_executor.McpToolExecutor -- the copy the engineer hit."""

    def _executor(self, tmp_path):
        return McpToolExecutor(
            mcp_server=None,
            job_id="j",
            task_id="t",
            agent_id="a",
            agent_role="backend_engineer",
            working_dir=str(tmp_path),
        )

    @pytest.mark.asyncio
    async def test_a_string_range_returns_the_lines_not_an_error(self, tmp_path, sample_file):
        out = await self._executor(tmp_path)._read_file({"path": "ActiveState.cpp", "start_line": "3", "end_line": "5"})

        assert out == "line3\nline4\nline5"

    @pytest.mark.asyncio
    async def test_a_string_range_matches_the_int_range(self, tmp_path, sample_file):
        ex = self._executor(tmp_path)

        as_str = await ex._read_file({"path": "ActiveState.cpp", "start_line": "3", "end_line": "5"})
        as_int = await ex._read_file({"path": "ActiveState.cpp", "start_line": 3, "end_line": 5})

        assert as_str == as_int

    @pytest.mark.asyncio
    async def test_an_unparseable_range_explains_itself_instead_of_raising(self, tmp_path, sample_file):
        out = await self._executor(tmp_path)._read_file({"path": "ActiveState.cpp", "start_line": "not-a-line"})

        assert "start_line" in json.loads(out)["error"]

    @pytest.mark.asyncio
    async def test_the_reread_counter_sees_two_spellings_as_one_range(self, tmp_path, sample_file):
        """The re-read accounting keys on the requested range. Keyed on the raw
        argument, "3" and 3 are two keys, so a model that varies its spelling
        re-reads for free and the waste metric under-reports."""
        ex = self._executor(tmp_path)

        await ex._read_file({"path": "ActiveState.cpp", "start_line": 3, "end_line": 5})
        await ex._read_file({"path": "ActiveState.cpp", "start_line": "3", "end_line": "5"})

        assert ex.read_stats()["reread_chars"] > 0

    @pytest.mark.asyncio
    async def test_distinct_ranges_are_still_distinct(self, tmp_path, sample_file):
        """The counter must not over-report either: different ranges are not re-reads."""
        ex = self._executor(tmp_path)

        await ex._read_file({"path": "ActiveState.cpp", "start_line": 1, "end_line": 2})
        await ex._read_file({"path": "ActiveState.cpp", "start_line": 5, "end_line": 6})

        assert ex.read_stats()["reread_chars"] == 0


class TestBothCopiesAgree:
    """The bug shipped twice because the code exists twice. If a third copy
    appears, or one copy is fixed alone, this is what notices."""

    @pytest.mark.asyncio
    async def test_the_two_executors_return_the_same_slice_for_a_string_range(self, tmp_path, sample_file):
        reviewer = ToolExecutor(provider=None, project_id="p", mr_id="1", repo_path=str(tmp_path))
        engineer = McpToolExecutor(
            mcp_server=None,
            job_id="j",
            task_id="t",
            agent_id="a",
            agent_role="backend_engineer",
            working_dir=str(tmp_path),
        )

        args = {"path": "ActiveState.cpp", "start_line": "2", "end_line": "4"}

        assert reviewer._read_file(dict(args)) == await engineer._read_file(dict(args))

    def test_no_read_file_implementation_indexes_a_raw_argument(self):
        """Source check, because a fourth copy would pass every test above by
        simply not being called. `args.get("start_line")` must go through the
        helper before it reaches arithmetic."""
        from minions.agents.tools import definitions, mcp_executor

        for path in (Path(definitions.__file__), Path(mcp_executor.__file__)):
            source = path.read_text(encoding="utf-8")
            start = source.index("def _read_file")
            body = source[start : source.index("\n    def ", start + 1)]

            assert "coerce_line_number" in body, f"{path.name} reads line numbers without coercing them"
            assert "max(1, args.get" not in body, f"{path.name} does arithmetic on a raw tool argument"
