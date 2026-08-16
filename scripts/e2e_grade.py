"""Grade a live e2e run: did the minions declare their guesses, and ground them?

Read-only. Run it after `task e2e:live` against the job that run produced.

What it deliberately does NOT do is check whether the agent chose the reading you
would have chosen. Most ambiguous tickets admit several defensible readings, so
grading on agreement rewards coincidence and punishes reasonable disagreement.
What is gradeable is whether a guess was declared, and whether the stated reason
points at something a human could go and check.

The last part is a judgement no script makes. This prints the evidence and
enforces only the mechanical checks; the reasons themselves are for you to read.

    uv run python scripts/e2e_grade.py [JOB_ID]

Exits non-zero if a mechanical check fails, so it can gate a CI job.
"""

import argparse
import asyncio
import sys

from minions.config import Config
from minions.core.models import JobStatus
from minions.core.spec_contract import extract_assumptions, has_assumptions

TERMINAL = {JobStatus.DONE, JobStatus.NO_WORK_NEEDED, JobStatus.FAILED}

# Events that mean the run degraded in a way the final status does not show.
CONCERNING_EVENTS = {
    "spec_refinement_skipped": "advanced on the raw ticket with no stated assumptions",
    "transition_rejected": "an agent proposed an illegal state transition",
    "job_cost_limit_exceeded": "ran out of budget",
    "job_rate_cap_deferred": "deferred by the rate cap",
}

# A reason is checkable when it points at something in the repo or at
# reversibility. These are heuristics for drawing your eye, never a verdict.
_GROUNDING_HINTS = ("matching", "matches", "existing", "convention", "already", "precedent", "reversible", "elsewhere", "consistent", "same as")


def _looks_grounded(line: str) -> bool:
    return any(hint in line.lower() for hint in _GROUNDING_HINTS)


async def grade(job_id: str | None) -> int:
    config = Config.from_env()
    from minions.cli import _create_db

    db = _create_db(config)
    await db.connect()

    try:
        if job_id:
            job = await db.get_job(job_id)
        else:
            # get_all_jobs is ordered created_at DESC (see postgres.py), so the
            # most recent run is first -- which is the one you just finished.
            jobs = await db.get_all_jobs()
            job = jobs[0] if jobs else None

        if job is None:
            print("No job found.", file=sys.stderr)
            return 2

        events = await db.get_events(job.id)
        seen = {e["event_type"] for e in events}
        original = job.original_spec or job.spec
        refined = job.spec
        block = extract_assumptions(refined)
        failures: list[str] = []

        print(f"\n=== Job {job.id} — {job.status} ===\n")
        print("--- Original ticket ---")
        print(original.strip() or "(empty)")

        print("\n--- Assumptions declared ---")
        if not block:
            print("(none)")
        else:
            for line in [ln for ln in block.splitlines() if ln.strip()]:
                mark = "  " if _looks_grounded(line) else "? "
                print(f"{mark}{line.rstrip()}")
            print("\n  '?' marks an assumption with no obvious grounding phrase — read those closely.")

        # -- mechanical checks --
        if job.original_spec is None:
            failures.append("spec was never refined (original_spec is null), so nothing was reasoned about")
        if not has_assumptions(refined):
            failures.append("refined spec carries no assumptions section")
        if job.status not in TERMINAL:
            failures.append(f"job did not reach a terminal state (stuck at {job.status})")

        print("\n--- Run health ---")
        for event, meaning in CONCERNING_EVENTS.items():
            if event in seen:
                print(f"  ! {event}: {meaning}")
                failures.append(f"{event}: {meaning}")
        if not (seen & set(CONCERNING_EVENTS)):
            print("  no degradation events recorded")

        agents = await db.get_agents_for_job(job.id)
        cost = sum(a.cost_usd or 0.0 for a in agents)
        turns = sum(a.num_turns or 0 for a in agents)
        print(f"  {len(agents)} agents, {turns} turns, ${cost:.4f}")

        print("\n--- Verdict ---")
        if failures:
            for f in failures:
                print(f"  FAIL  {f}")
            print("\nMechanical checks failed. The assumptions themselves still need a human read.")
            return 1
        print("  mechanical checks passed")
        print("\nNow read the assumptions above against the repo. A stated reason that")
        print("does not survive checking is worse than no reason at all.")
        return 0
    finally:
        await db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Grade a live e2e run.")
    parser.add_argument("job_id", nargs="?", help="Job to grade (default: most recent)")
    args = parser.parse_args()
    sys.exit(asyncio.run(grade(args.job_id)))


if __name__ == "__main__":
    main()
