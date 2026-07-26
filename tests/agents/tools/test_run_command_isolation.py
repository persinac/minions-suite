"""An agent's shell command must not be able to kill the server running it.

In-process agents execute inside the server container, where the server is
PID 1. `_run_command` used `create_subprocess_shell` with no session isolation,
so the shell and everything it spawned shared PID 1's process group. Any command
that signals its own group — `kill 0`, a test harness tearing down workers, a
Makefile cleanup trap, `pkill -f python` — delivered that signal to the MCP
server as well.

Measured, not theorised: a parent whose child runs `kill 0` exits **143**
(128 + SIGTERM) when they share a group, and survives with
`start_new_session=True`.

143 is exactly what was happening. uvicorn handles SIGTERM by shutting down
gracefully and exiting 0, so the pod entered Completed rather than Error, the
ReplicaSet replaced it at restarts=0, and every crash-shaped diagnostic came
back clean — no OOMKilled, no eviction, no restart, no probe failure, no node
pressure. It presented as unexplained pod churn for hours, and cost five jobs.

The timeout path had a second defect: it returned an error while leaving the
command running, so a leaked pytest or dev server kept burning CPU and holding
the workspace for the rest of the pod's life while the agent was told it had
timed out.
"""

import asyncio
import inspect
import os
import sys

import pytest

from minions.agents.tools.mcp_executor import _kill_process_group, _reap


class TestSessionIsolation:
    @pytest.mark.asyncio
    async def test_a_child_killing_its_group_cannot_reach_us(self):
        """The regression, executed. `kill 0` signals the caller's process
        group; with its own session the child cannot reach this process."""
        proc = await asyncio.create_subprocess_shell(
            "kill 0",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        await asyncio.wait_for(proc.wait(), timeout=10)

        # Reaching this line at all is the assertion: without isolation the
        # test process would have died with SIGTERM instead.
        assert True

    @pytest.mark.asyncio
    async def test_the_child_really_is_in_its_own_group(self):
        proc = await asyncio.create_subprocess_shell(
            "sleep 5",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            assert os.getpgid(proc.pid) != os.getpgid(os.getpid())
            assert os.getpgid(proc.pid) == proc.pid, "session leader's pgid is its own pid"
        finally:
            _kill_process_group(proc)
            await _reap(proc)


class TestTimeoutCleanup:
    @pytest.mark.asyncio
    async def test_a_timed_out_command_is_actually_killed(self):
        """It used to be left running — the agent was told it timed out while
        the work continued underneath it."""
        proc = await asyncio.create_subprocess_shell(
            "sleep 60",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )

        _kill_process_group(proc)
        await _reap(proc)

        assert proc.returncode is not None, "process must be dead and reaped"

    @pytest.mark.asyncio
    async def test_killing_an_already_dead_process_is_safe(self):
        proc = await asyncio.create_subprocess_shell(
            "true", stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, start_new_session=True
        )
        await proc.wait()

        _kill_process_group(proc)  # must not raise
        await _reap(proc)

    @pytest.mark.asyncio
    async def test_the_whole_tree_dies_not_just_the_shell(self):
        """`sh -c "a | b"` leaves children that outlive their parent shell, so
        killing only the shell leaks the grandchildren."""
        proc = await asyncio.create_subprocess_shell(
            f"{sys.executable} -c 'import time; time.sleep(60)' & sleep 60",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        await asyncio.sleep(0.5)
        pgid = os.getpgid(proc.pid)

        _kill_process_group(proc)
        await _reap(proc)
        await asyncio.sleep(0.3)

        with pytest.raises(ProcessLookupError):
            os.killpg(pgid, 0)  # signal 0 probes existence


class TestWiring:
    def test_run_command_isolates_the_session(self):
        from minions.agents.tools.mcp_executor import McpToolExecutor

        source = inspect.getsource(McpToolExecutor._run_command)

        assert "start_new_session=True" in source

    def test_run_command_kills_on_timeout(self):
        from minions.agents.tools.mcp_executor import McpToolExecutor

        source = inspect.getsource(McpToolExecutor._run_command)

        assert "_kill_process_group(proc)" in source
        assert "await _reap(proc)" in source
