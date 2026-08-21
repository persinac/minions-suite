"""Start a herder when work is waiting, so the subscription path runs itself.

`engineer_dispatch = "external"` makes the engine publish a work item and run
nothing. The `herd` skill is the subscription-billed claimant. Nothing ever
started it, so after `herder_claim_timeout_seconds` (900s) the engine gave up and
ran the engineer in-process on the metered API -- every time, not occasionally.
This is the missing piece: poll for waiting work, spawn a herder pane, let the
Claude session claim it.

A spawned pane is also this script's to CLOSE. A Claude session does not exit
when its work is done -- it sits at the prompt -- so without a reaper every work
item leaks a pane, and a revision round leaks another for the same task. Each
tick reaps before it spawns; see `reap_plan`.

    herder_trigger.py --once      one tick, then exit
    herder_trigger.py --watch     poll forever
    herder_trigger.py --status    what it would see right now
    herder_trigger.py --reap      close finished panes, spawn nothing

Safety follows conductor-run.sh: OFF unless the host opts in with
`MINIONS_HERDER_MODE=live`, because a trigger that spawns agents unattended is
not something a work laptop should inherit from a git pull.

    MINIONS_HERDER_MODE   off (default) | dry | live
    MINIONS_HERDER_MAX    concurrent herders (default 2)
    MINIONS_HERDER_PANE_TTL  seconds before a stuck pane is reaped (default 2700)
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

# Backstop for a pane whose claim still reads live but which has stopped making
# progress -- a hung or wedged herder that no other rule can distinguish from a
# working one. Deliberately the same 2700s as the engine's
# `herder_work_timeout_seconds` (minions/config.py): that is the point at which
# the engine ALREADY presumes the worker gone and re-offers the task, so reaping
# here neither races it nor lets a dead pane outlive its claim.
PANE_TTL_SECONDS = int(os.environ.get("MINIONS_HERDER_PANE_TTL", "2700"))

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


def reap_plan(state: dict, waiting_ids: set[str], live_task_ids: set[str], now: float) -> tuple[dict, list[str]]:
    """Decide which panes to close. Returns (state_to_keep, pane_ids_to_kill).

    Pure on purpose: the rule is the part worth testing, and it must be testable
    without herdr, an MCP server, or a real pane.

    A pane is kept only while it is plausibly doing something:

    - it holds a LIVE claim (its task has a running herder agent), or
    - it is still inside its spawn window and its task is still WAITING, i.e.
      it has started up but not claimed yet.

    Everything else is finished, dead, or unaccountable, and gets closed. That
    includes a pane whose work succeeded -- the workspace is meant to end up
    empty, so success and failure are reaped alike and a failed run is read back
    from get_agent_log rather than from a live pane.

    PANE_TTL is the backstop for a herder that claimed and then hung: the claim
    still reads live, so no other rule fires. It is deliberately the same value
    as the engine's `herder_work_timeout_seconds`, the point at which the engine
    itself already presumes the worker gone -- reaping earlier than the engine
    would hand live work to the metered path, which is the cost this whole
    mechanism exists to avoid.
    """
    keep: dict = {}
    kill: list[str] = []

    for pane_id, entry in state.items():
        # A legacy `{task_id: timestamp}` entry has no pane to close and no way
        # to learn one -- herdr uniquifies spawn names, so it cannot be derived.
        # Drop it rather than carrying it forever.
        if not isinstance(entry, dict):
            continue

        task_id = entry.get("task_id", "")
        age = now - float(entry.get("at", 0))

        if age >= PANE_TTL_SECONDS:
            kill.append(pane_id)
            continue
        if task_id in live_task_ids:
            keep[pane_id] = entry
            continue
        if task_id in waiting_ids and age < SPAWN_TTL_SECONDS:
            keep[pane_id] = entry
            continue
        kill.append(pane_id)

    return keep, kill


def kill_pane(pane_id: str) -> bool:
    """Close one pane. A failure is logged and retried next tick, never dropped."""
    try:
        done = subprocess.run([str(SUBSTRATE), "kill", pane_id], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"reap failed for {pane_id}: {type(exc).__name__}: {exc}")
        return False
    if done.returncode != 0:
        log(f"reap failed for {pane_id} (rc={done.returncode}): {done.stderr.strip()[:160]}")
        return False
    log(f"reaped {pane_id}")
    return True


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


async def live_claims() -> list[dict]:
    """Herder claims still running. The reaper's "is this pane still working?".

    Kept separate from peek() because they answer opposite questions: peek is
    work with NO owner, this is work whose owner is still alive. A pane is
    finished precisely when it appears in neither.
    """
    from fastmcp import Client

    async with Client(MCP_URL) as client:
        result = await client.call_tool("herder_status", {})
        payload = json.loads(result.content[0].text)
    return payload.get("live", [])


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


def parse_pane_id(stdout: str) -> str:
    """Pull the herdr pane id out of `substrate.sh spawn --print`.

    That prints herdr's whole JSON envelope, not a bare id:
    {"id":"cli:agent:start","result":{"agent":{...,"pane_id":"w11:pA",...}}}
    and `substrate.sh kill` wants the pane_id from inside it. Verified against a
    real spawn; returns "" rather than raising, because an unparseable envelope
    means "cannot reap this" and the caller says so out loud.
    """
    try:
        payload = json.loads(stdout.strip())
    except ValueError, TypeError:
        return ""
    agent = payload.get("result", {}).get("agent", {})
    pane_id = agent.get("pane_id", "")
    if not isinstance(pane_id, str):
        return ""
    return pane_id


def spawn(item: dict, dry: bool) -> str | None:
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
        # Without this the id is never printed and the pane cannot be reaped
        # later. The NAME is not a substitute: herdr uniquifies on collision
        # (<name>, <name>-2, ...), and collisions are the normal case here
        # because a revision round spawns a second herder for the SAME task.
        "--print",
    ]

    if dry:
        log(f"DRY would spawn {name} in {cwd} (workspace {WORKSPACE})")
        return None

    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"spawn failed for {name}: {exc}")
        return None

    if done.returncode != 0:
        log(f"spawn failed for {name} (rc={done.returncode}): {done.stderr.strip()[:200]}")
        return None

    pane_id = parse_pane_id(done.stdout)
    if not pane_id:
        # The pane is running but unreapable. Say so loudly rather than tracking
        # it under a fake key: a silent miss here is exactly the leak this whole
        # change exists to close.
        log(f"spawned {name} but could NOT parse a pane id — it will not be reaped: {done.stdout.strip()[:160]}")
        return None

    log(f"spawned {name} ({pane_id}) in {cwd} — {item.get('title', '')[:60]}")
    return pane_id


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

    try:
        live = await live_claims()
    except Exception as exc:
        # Reaping needs this; spawning does not. Treating a failed lookup as
        # "nothing is live" would close every working pane, so bail instead.
        log(f"herder_status failed: {type(exc).__name__}: {exc} — skipping tick rather than reaping blind")
        return 0

    now = time.time()
    waiting_ids = {w["task_id"] for w in waiting}
    live_task_ids = {c["task_id"] for c in live if c.get("task_id")}

    # Reap FIRST: a finished pane still occupies a MAX_HERDERS slot, so freeing
    # it here lets the same pass spawn for work that would otherwise wait a full
    # poll interval.
    state, doomed = reap_plan(load_state(), waiting_ids, live_task_ids, now)
    for pane_id in doomed:
        if MODE != "live":
            log(f"DRY would reap {pane_id}")
            continue
        kill_pane(pane_id)

    if not waiting:
        save_state(state)
        return 0

    tracked_tasks = {e.get("task_id") for e in state.values() if isinstance(e, dict)}

    spawned = 0
    for item in waiting:
        task_id = item["task_id"]
        if task_id in tracked_tasks:
            continue  # already spawned; it has not claimed yet
        if len(state) >= MAX_HERDERS:
            log(f"at MINIONS_HERDER_MAX={MAX_HERDERS} — {len(waiting) - spawned} item(s) still waiting")
            break
        is_dry = MODE != "live"
        pane_id = spawn(item, dry=is_dry)
        # Only a REAL spawn consumes the budget. Recording a dry run here made
        # `dry` and then `live` silently do nothing: the dry tick marked the task
        # as already-spawned, and the live tick skipped it as work someone else
        # had taken. That breaks the one workflow the dry mode exists to support
        # -- look at what it would do, then let it do it.
        if is_dry:
            spawned += 1
            continue
        if not pane_id:
            continue
        spawned += 1
        state[pane_id] = {"task_id": task_id, "at": now}
        tracked_tasks.add(task_id)

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
    print(f"pane ttl:  {PANE_TTL_SECONDS}s")
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

    try:
        live = await live_claims()
    except Exception as exc:
        print(f"\nclaims:    herder_status failed: {type(exc).__name__}: {exc}")
        return 1
    print(f"\nclaims:    {len(live)} live")
    for c in live:
        print(f"  {str(c.get('task_id', ''))[:8]}  worker={c.get('worker', '?'):<22} {c.get('status', '')}")

    now = time.time()
    _keep, doomed = reap_plan(state, {w["task_id"] for w in waiting}, {c["task_id"] for c in live if c.get("task_id")}, now)
    print(f"\nreapable:  {len(doomed)} {doomed}")
    return 0


async def reap_once() -> int:
    """Sweep finished panes without spawning anything."""
    if not tunnel_healthy():
        log("MCP tunnel is down — refusing to reap (a dead tunnel reads as no live claims)")
        return 1
    try:
        waiting = await peek()
        live = await live_claims()
    except Exception as exc:
        log(f"lookup failed: {type(exc).__name__}: {exc} — not reaping blind")
        return 1

    state, doomed = reap_plan(load_state(), {w["task_id"] for w in waiting}, {c["task_id"] for c in live if c.get("task_id")}, time.time())
    for pane_id in doomed:
        if MODE != "live":
            log(f"DRY would reap {pane_id}")
            continue
        kill_pane(pane_id)
    save_state(state)
    log(f"reap: {len(doomed)} pane(s), {len(state)} still tracked")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Spawn a herder when minions work is waiting.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--once", action="store_true", help="one tick, then exit")
    group.add_argument("--watch", action="store_true", help="poll forever")
    group.add_argument("--status", action="store_true", help="show what it sees, change nothing")
    group.add_argument("--reap", action="store_true", help="close finished panes, spawn nothing")
    args = parser.parse_args()

    if args.status:
        sys.exit(asyncio.run(status()))
    if args.reap:
        sys.exit(asyncio.run(reap_once()))
    if args.once:
        asyncio.run(tick())
        sys.exit(0)
    sys.exit(asyncio.run(watch()))


if __name__ == "__main__":
    main()
