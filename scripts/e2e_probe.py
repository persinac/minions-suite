"""Run a real ticket through the real spec analyst and arbiter, and stop there.

The point is to watch how the minions handle an ambiguous ticket without needing
a repository to handle it against.

That works because the reasoning this exercises happens before any code is
touched. `launch_spec_analyst` reads the ticket and writes a refined spec;
`launch_arbiter` decomposes that spec into tasks. Neither opens a working tree,
clones anything, or talks to a git provider -- only `run_engineer` does, and this
script stops before it. So a probe costs a few cents and a minute, needs no
sandbox repo and no git credentials, and still answers the question that actually
matters: given a vague ticket, does the analyst notice what is missing, and does
it say so?

    uv run python scripts/e2e_probe.py missing-bound
    uv run python scripts/e2e_probe.py missing-bound --spec-only

This is real: real model, real cost, real database rows. It is not a dry run and
not a test double. What it is not is *complete* -- a full run also exercises the
engineers, the reviewer and the merge gate, and those still need a scratch repo.
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from minions.config import Config
from minions.core.models import JobStatus
from minions.core.spec_contract import extract_assumptions

TICKETS = Path(__file__).resolve().parents[1] / "tests" / "e2e" / "tickets"


def _resolve_ticket(name: str) -> Path:
    candidate = Path(name)
    if candidate.is_file():
        return candidate
    candidate = TICKETS / f"{name}.md"
    if candidate.is_file():
        return candidate
    available = sorted(p.stem for p in TICKETS.glob("*.md") if p.stem != "README")
    raise SystemExit(f"No such ticket: {name}. Available: {', '.join(available)}")


async def probe(ticket: Path, spec_only: bool) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    from minions.cli import _create_db
    from minions.engine import dev
    from minions.engine.job_engine import JobEngine
    from minions.server.mcp import create_server

    config = Config.from_env()
    db = _create_db(config)
    await db.connect()

    try:
        text = ticket.read_text(encoding="utf-8")
        print(f"\n{'=' * 70}\nTICKET: {ticket.name}\n{'=' * 70}\n{text.strip()}\n")

        job = await db.create_job(text)
        print(f"Job {job.id} created. Running spec analyst against {config.model} ...\n")

        engine = JobEngine(db=db, config=config, mcp_server=create_server(db, config))
        await dev.launch_spec_analyst(engine, job)

        job = await db.get_job(job.id)
        print(f"\n{'=' * 70}\nREFINED SPEC  (job now {job.status})\n{'=' * 70}")
        print(job.spec.strip() or "(empty)")

        assumptions = extract_assumptions(job.spec)
        print(f"\n{'=' * 70}\nASSUMPTIONS DECLARED\n{'=' * 70}")
        print(assumptions.strip() or "(NONE — the analyst filled the gaps silently)")

        if not spec_only and job.status == JobStatus.SPEC_READY:
            print(f"\n{'=' * 70}\nARBITER\n{'=' * 70}")
            await dev.launch_arbiter(engine, job)
            tasks = [t for t in await db.get_tasks(job.id) if t.service not in ("_spec", "_arbiter")]
            if not tasks:
                print("(no tasks created)")
            for t in tasks:
                print(f"\n[{t.agent_role}] {t.title}  (service: {t.service})")
                print(f"  {t.description.strip()[:600]}")

        agents = await db.get_agents_for_job(job.id)
        cost = sum(a.cost_usd or 0.0 for a in agents)
        turns = sum(a.num_turns or 0 for a in agents)
        print(f"\n{'=' * 70}\n{len(agents)} agents, {turns} turns, ${cost:.4f}")
        print(f"Grade with:  uv run python scripts/e2e_grade.py {job.id}")
        return 0
    finally:
        await db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Probe how the minions handle one ambiguous ticket.")
    parser.add_argument("ticket", help="Ticket name (see tests/e2e/tickets/) or a path")
    parser.add_argument("--spec-only", action="store_true", help="Stop after the spec analyst; skip the arbiter")
    args = parser.parse_args()
    sys.exit(asyncio.run(probe(_resolve_ticket(args.ticket), args.spec_only)))


if __name__ == "__main__":
    main()
