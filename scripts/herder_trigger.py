"""Start a herder when work is waiting, so the subscription path runs itself.

`engineer_dispatch = "external"` makes the engine publish a work item and run
nothing. The `herd` skill is the subscription-billed claimant. Nothing ever
started it, so after `herder_claim_timeout_seconds` (900s) the engine gave up and
ran the engineer in-process on the metered API -- every time, not occasionally.
This is the missing piece: poll for waiting work, spawn a herder pane, let the
Claude session claim it.

    herder_trigger.py --once      one tick, then exit
    herder_trigger.py --watch     poll forever
    herder_trigger.py --status    what it would see right now

Safety follows conductor-run.sh: OFF unless the host opts in with
`MINIONS_HERDER_MODE=live`, because a trigger that spawns agents unattended is
not something a work laptop should inherit from a git pull.

    MINIONS_HERDER_MODE   off (default) | dry | live
    MINIONS_HERDER_MAX    concurrent herders (default 2)
    MINIONS_MCP_URL       default http://127.0.0.1:8321/sse
"""

import argparse
import asyncio
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

MCP_URL = os.environ.get("MINIONS_MCP_URL", "http://127.0.0.1:8321/sse")
MODE = os.environ.get("MINIONS_HERDER_MODE", "off").strip().lower()
MAX_HERDERS = int(os.environ.get("MINIONS_HERDER_MAX", "2"))
POLL_SECONDS = int(os.environ.get("MINIONS_HERDER_POLL_SECONDS", "30"))

NEXUS_DIR = Path(os.environ.get("NEXUS_TMUX_DIR", str(Path.home() / ".tmux")))
SUBSTRATE = NEXUS_DIR / "substrate.sh"
STATE_DIR = NEXUS_DIR / "minions"
STATE_FILE = STATE_DIR / "spawned.json"
FORWARD_CHECK = Path(__file__).resolve().parent / "mcp_forward.sh"

# How long a spawn is presumed to still be starting up. A pane takes a few
# seconds to reach its first tool call, and until the session calls
# claim_engineer_work the task is STILL visible to peek -- so without this
# window every tick in that gap would spawn another herder for the same task.
SPAWN_TTL_SECONDS = int(os.environ.get("MINIONS_HERDER_SPAWN_TTL", "600"))

WORKSPACE = os.environ.get("MINIONS_HERDER_WORKSPACE", "minions/herd")

# The whole point is a herder nobody has to attend, and every permission POSTURE
# has its own first-run dialog that a spawned pane will sit on forever:
#   manual            -> "Do you want to proceed?" at the first tool call
#   bypassPermissions -> "you accept all responsibility" consent, persisted
#                        nowhere in ~/.claude.json, so it returns every session
#   auto              -> "Set up auto mode for your environment?" onboarding
#
# All three observed on real panes. `--dangerously-skip-permissions` is the only
# one that comes up running, and it is a FLAG rather than a mode -- hence
# CLAUDE_EXTRA_ARGS rather than CLAUDE_PERMISSION_MODE.
#
# It IS a real widening: the herder writes code, pushes a branch and opens a PR
# without asking. That is what was asked for, and it is bounded elsewhere --
# MINIONS_HERDER_MODE gates whether any of this runs at all, the concurrency cap
# bounds how many, and the reviewers still gate the merge.
CLAUDE_EXTRA_ARGS = os.environ.get("MINIONS_HERDER_CLAUDE_ARGS", "--dangerously-skip-permissions")

SEED_PROMPT = (
    "You are the herder. Run the /herd skill now: claim the waiting minions "
    "engineering work item, implement it, and report back over MCP. "
    "Task {task_id} ({service}) is waiting. Do not ask for confirmation."
)


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


# ---------------------------------------------------------------------------
# State: which tasks we have already spawned for
# ---------------------------------------------------------------------------


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except OSError, json.JSONDecodeError:
        return {}


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def prune(state: dict, waiting_ids: set[str], now: float) -> dict:
    """Drop entries that are finished or stale.

    A task that has left the waiting list was claimed -- by the herder we
    spawned, or by anything else -- so the entry has done its job. Entries also
    expire on TTL, otherwise a pane that died before claiming would block that
    task from ever being retried.
    """
    return {task_id: at for task_id, at in state.items() if task_id in waiting_ids and (now - at) < SPAWN_TTL_SECONDS}


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------


def tunnel_healthy() -> bool:
    """Whether the MCP tunnel is up. Never guess from an empty queue.

    Invoked through `bash` rather than executed directly, so a missing exec bit
    cannot masquerade as a dead tunnel. It already did once: a chmod that never
    ran made this raise PermissionError, which the handler below turned into
    "tunnel DOWN" for a tunnel that was serving fine — the same conflation of
    two different failures this function exists to prevent, arriving by a
    different door.
    """
    try:
        return subprocess.run(["bash", str(FORWARD_CHECK), "--check"], capture_output=True, timeout=10).returncode == 0
    except OSError, subprocess.SubprocessError:
        return False


async def peek() -> list[dict]:
    """Ask what is waiting. Deliberately peek, never claim.

    claim_engineer_work takes ownership as a side effect of asking, so polling
    with it would strand one task per tick for 900s.
    """
    from fastmcp import Client

    async with Client(MCP_URL) as client:
        result = await client.call_tool("peek_engineer_work", {})
        payload = json.loads(result.content[0].text)
    return payload.get("waiting", [])


# ---------------------------------------------------------------------------
# Spawn
# ---------------------------------------------------------------------------


def working_dir(item: dict) -> str:
    """Where to start the pane: the minions-suite checkout, not the target repo.

    `/herd` is a PROJECT skill -- it lives in this repo's .claude/skills/ and is
    not symlinked into ~/.claude/skills/. A pane started in the target service's
    tree would have no /herd at all, and the seed prompt telling it to run /herd
    would find nothing. That was this function's first version, and it would have
    failed on the first live spawn.

    Starting here costs nothing, because the skill does not want the target tree
    anyway: it clones from clone_url and is explicit that a local clone must be
    used through a git worktree rather than by switching branches in a tree that
    may be someone's working copy.

    Derived from this file's own location so it is right for any checkout,
    including a worktree of it.
    """
    return str(Path(__file__).resolve().parents[1])


def spawn(item: dict, dry: bool) -> bool:
    task_id = item["task_id"]
    name = f"minions-herd-{task_id[:8]}"
    cwd = working_dir(item)
    seed = SEED_PROMPT.format(task_id=task_id, service=item.get("service", "?"))
    # open-claude.sh reads SEED_PROMPT for "a task to begin on immediately", and
    # going through it (rather than bare `claude`) is what registers the agent in
    # the fleet registry and on the Slack bus.
    #
    # ONE argument, not `sh -c <cmd>`. substrate.sh joins everything after <cwd>
    # into a single string and wraps it in its OWN `sh -c`, so passing a
    # pre-wrapped `sh -c ...` double-wraps: the joined string became
    # `sh -c SEED_PROMPT="You are the herder. ...`, the inner shell word-split on
    # the first space, took `SEED_PROMPT="You` as its whole command, and exited
    # instantly. herdr reported success and the agent vanished before the next
    # poll -- a spawn that returns 0 and leaves nothing behind.
    #
    # substrate.sh's own comment anticipates this shape: the command string "may
    # carry an inline `env VAR='multi word value' prog` prefix (e.g.
    # SEED_PROMPT)", which is precisely what this is.
    command = (
        f"SEED_PROMPT={shlex.quote(seed)} CLAUDE_EXTRA_ARGS={shlex.quote(CLAUDE_EXTRA_ARGS)} exec {shlex.quote(str(NEXUS_DIR / 'open-claude.sh'))}"
    )
    argv = [
        str(SUBSTRATE),
        "spawn",
        name,
        cwd,
        command,
        "--workspace",
        WORKSPACE,
    ]

    if dry:
        log(f"DRY would spawn {name} in {cwd} (workspace {WORKSPACE})")
        return True

    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"spawn failed for {name}: {exc}")
        return False

    if done.returncode != 0:
        log(f"spawn failed for {name} (rc={done.returncode}): {done.stderr.strip()[:200]}")
        return False

    log(f"spawned {name} in {cwd} — {item.get('title', '')[:60]}")
    return True


# ---------------------------------------------------------------------------
# Tick
# ---------------------------------------------------------------------------


async def tick() -> int:
    """One pass. Returns the number of herders spawned."""
    if MODE == "off":
        log("MINIONS_HERDER_MODE is off — set it to live (or dry) in ~/.tmux/env.sh")
        return 0

    if not tunnel_healthy():
        # Deliberately not "no work": a dead tunnel and an empty queue look
        # identical from here, and treating one as the other is how the metered
        # fallback fires while the trigger reports everything is fine.
        log("MCP tunnel is down — skipping tick (run scripts/mcp_forward.sh)")
        return 0

    try:
        waiting = await peek()
    except Exception as exc:
        log(f"peek failed: {type(exc).__name__}: {exc}")
        return 0

    now = time.time()
    state = prune(load_state(), {w["task_id"] for w in waiting}, now)

    if not waiting:
        save_state(state)
        return 0

    spawned = 0
    for item in waiting:
        task_id = item["task_id"]
        if task_id in state:
            continue  # already spawned; it has not claimed yet
        if len(state) >= MAX_HERDERS:
            log(f"at MINIONS_HERDER_MAX={MAX_HERDERS} — {len(waiting) - spawned} item(s) still waiting")
            break
        is_dry = MODE != "live"
        if not spawn(item, dry=is_dry):
            continue
        spawned += 1
        # Only a REAL spawn consumes the budget. Recording a dry run here made
        # `dry` and then `live` silently do nothing: the dry tick marked the task
        # as already-spawned, and the live tick skipped it as work someone else
        # had taken. That breaks the one workflow the dry mode exists to support
        # -- look at what it would do, then let it do it.
        if not is_dry:
            state[task_id] = now

    save_state(state)
    return spawned


async def watch() -> int:
    log(f"herder trigger watching (mode={MODE}, max={MAX_HERDERS}, every {POLL_SECONDS}s)")
    while True:
        try:
            await tick()
        except Exception as exc:
            # One bad tick must not end the watch; the next one may well work.
            log(f"tick error: {type(exc).__name__}: {exc}")
        await asyncio.sleep(POLL_SECONDS)


async def status() -> int:
    print(f"mode:      {MODE}")
    print(f"max:       {MAX_HERDERS}")
    print(f"mcp:       {MCP_URL}")
    print(f"tunnel:    {'up' if tunnel_healthy() else 'DOWN'}")
    print(f"substrate: {SUBSTRATE} {'(present)' if SUBSTRATE.exists() else '(MISSING)'}")
    state = load_state()
    print(f"spawned:   {len(state)} tracked {list(state)}")
    if not tunnel_healthy():
        print("\nqueue:     unknown — tunnel down")
        return 1
    try:
        waiting = await peek()
    except Exception as exc:
        print(f"\nqueue:     peek failed: {type(exc).__name__}: {exc}")
        return 1
    print(f"\nqueue:     {len(waiting)} waiting")
    for w in waiting:
        print(f"  {w['task_id'][:8]}  {w['service']:<24} {w.get('title', '')[:50]}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Spawn a herder when minions work is waiting.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--once", action="store_true", help="one tick, then exit")
    group.add_argument("--watch", action="store_true", help="poll forever")
    group.add_argument("--status", action="store_true", help="show what it sees, change nothing")
    args = parser.parse_args()

    if args.status:
        sys.exit(asyncio.run(status()))
    if args.once:
        asyncio.run(tick())
        sys.exit(0)
    sys.exit(asyncio.run(watch()))


if __name__ == "__main__":
    main()
