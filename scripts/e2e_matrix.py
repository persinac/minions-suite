"""Run one ticket corpus across several models, so a cheap model can be judged fairly.

The per-model numbers in `minion --effectiveness` are suggestive but confounded:
the difficulty classifier already routes easy tickets to the cheap tier, so a
higher failure rate on haiku partly measures ticket mix rather than the model.
This removes that confound the only way it can be removed -- run the SAME
tickets through each model and compare.

    uv run python scripts/e2e_matrix.py --models claude-haiku-4-5,claude-sonnet-5
    uv run python scripts/e2e_matrix.py --report exp-2026-08-26

WHAT THIS MEASURES, AND WHAT IT DOES NOT.

The default `probe` mode runs the spec analyst and the arbiter, and stops --
the same boundary as scripts/e2e_probe.py, and for the same reason: neither
role opens a working tree, so no sandbox repo and no git credentials are
needed. That covers roughly 7% of real spend by role.

It does NOT run the engineer (~46% of spend) or the reviewer (~40%). So this
answers "does a cheaper model reason worse about an ambiguous ticket", NOT
"does a cheaper model write worse code". Do not generalise a probe result to
the engineer -- the engineer's workload is input-dominated and long-horizon,
which is exactly where cheap models are known to degrade differently.

For the full path use `--mode live`, which needs a throwaway repo wired into
projects.yaml first: it opens real PRs.

WHERE THE ROWS GO. Every run writes a real job to whatever POSTGRES_URL points
at, and settings.toml points at the DEPLOYED database. Use the local one:

    POSTGRES_URL=postgresql://minion:minion@localhost:5434/minion \\
        uv run python scripts/e2e_matrix.py --models ...
"""

import argparse
import asyncio
import importlib.util
import logging
import sys
from collections import defaultdict
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# pyproject.toml declares no [build-system], so `minions` is never installed into
# the venv -- it imports only because the project root happens to be on sys.path.
# That holds for `python -m minions` and `python -c`, but NOT for a script file,
# where sys.path[0] is scripts/ instead of the cwd. Without this line every
# script in here dies on `import minions`.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from minions.config import Config  # noqa: E402
from minions.core.spec_contract import extract_assumptions, has_assumptions  # noqa: E402

TICKETS = ROOT / "tests" / "e2e" / "tickets"
EXPERIMENT_EVENT = "experiment_variant"


def _load_grader():
    """Borrow the grading heuristics from e2e_grade.py without moving them.

    scripts/ is not a package, so a plain import will not reach it. Loading by
    path keeps one definition of "does this assumption look grounded" rather
    than a second copy here that drifts from the one you actually read output
    from.
    """
    spec = importlib.util.spec_from_file_location("e2e_grade", ROOT / "scripts" / "e2e_grade.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _resolve_tickets(names: str) -> list[Path]:
    available = sorted(p for p in TICKETS.glob("*.md") if p.stem != "README")
    if names in ("all", ""):
        return available
    out = []
    for name in names.split(","):
        name = name.strip()
        candidate = TICKETS / f"{name}.md"
        if not candidate.is_file():
            raise SystemExit(f"No such ticket: {name}. Available: {', '.join(p.stem for p in available)}")
        out.append(candidate)
    return out


def _pin_model(config: Config, model: str) -> Config:
    """Force every role onto one model.

    Each of these is a separate branch in resolve_model() (classifier.py), and
    missing one silently leaves that role on its default -- which would look
    like the model under test performing suspiciously well or badly on whatever
    that role does. The classifier is disabled too: with all tiers pinned it
    cannot change the outcome, so it would only add a per-job cost and one more
    thing that differs between arms.
    """
    return replace(
        config,
        model=model,
        model_easy=model,
        model_medium=model,
        model_hard=model,
        model_reviewer=model,
        model_engineer=model,
        model_finisher=model,
        classifier_enabled=False,
    )


async def _run_one(config: Config, db, ticket: Path, model: str, experiment: str) -> dict:
    """One (model, ticket) cell. Returns its scorecard row."""
    from minions.engine import dev
    from minions.engine.job_engine import JobEngine
    from minions.server.mcp import create_server

    text = ticket.read_text(encoding="utf-8")
    job = await db.create_job(text)
    # Attribution marker. Deliberately an event and not external_id: the Trello
    # newest-job lookup keys on external_id (#53), and squatting on it here
    # would let an experiment row shadow a real card.
    await db.record_event(job.id, EXPERIMENT_EVENT, "e2e_matrix", f"{experiment}|{model}|{ticket.stem}")

    engine = JobEngine(db=db, config=config, mcp_server=create_server(db, config))
    error = None
    try:
        await dev.launch_spec_analyst(engine, job)
        job = await db.get_job(job.id)
        if job.status == "spec_ready":
            await dev.launch_arbiter(engine, job)
    except Exception as e:
        error = f"{type(e).__name__}: {e}"[:200]

    job = await db.get_job(job.id)
    agents = await db.get_agents_for_job(job.id)
    events = {e["event_type"] for e in await db.get_events(job.id)}
    tasks = [t for t in await db.get_tasks(job.id) if t.service not in ("_spec", "_arbiter")]
    return {
        "model": model,
        "ticket": ticket.stem,
        "job_id": job.id,
        "status": str(job.status),
        "cost_usd": sum(a.cost_usd or 0.0 for a in agents),
        "turns": sum(a.num_turns or 0 for a in agents),
        "agents": len(agents),
        "agents_failed": sum(1 for a in agents if a.status == "failed"),
        "refined": job.original_spec is not None,
        "spec": job.spec or "",
        "tasks_created": len(tasks),
        "events": events,
        "error": error,
    }


def _score(row: dict, grader) -> dict:
    """Turn one run into gradeable numbers."""
    block = extract_assumptions(row["spec"])
    items = grader._assumption_items(block)
    grounded = sum(1 for i in items if grader._looks_grounded(i))
    degraded = sorted(row["events"] & set(grader.CONCERNING_EVENTS))
    return {
        **row,
        "assumptions": len(items),
        "grounded": grounded,
        "has_section": has_assumptions(row["spec"]),
        "degraded": degraded,
    }


async def run_matrix(models: list[str], tickets: list[Path], experiment: str, mode: str) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    if mode != "probe":
        raise SystemExit("--mode live is not wired here yet; use `task e2e:live` per ticket against a throwaway repo.")

    from minions.cli import _create_db

    base = Config.from_env()
    grader = _load_grader()
    db = _create_db(base)
    await db.connect()

    rows: list[dict] = []
    try:
        total = len(models) * len(tickets)
        n = 0
        for model in models:
            config = _pin_model(base, model)
            for ticket in tickets:
                n += 1
                print(f"[{n}/{total}] {model} x {ticket.stem} ...", flush=True)
                row = _score(await _run_one(config, db, ticket, model, experiment), grader)
                rows.append(row)
                flag = ""
                if row["error"]:
                    flag = f"  ERROR {row['error']}"
                print(
                    f"        ${row['cost_usd']:.4f}  {row['turns']:>3} turns  "
                    f"{row['assumptions']} assumptions ({row['grounded']} grounded)  "
                    f"{row['tasks_created']} tasks  job={row['job_id']}{flag}",
                    flush=True,
                )
    finally:
        await db.close()

    _print_report(rows, experiment)
    return 0


def _print_report(rows: list[dict], experiment: str) -> None:
    by_model: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_model[r["model"]].append(r)

    print(f"\n{'=' * 78}\nEXPERIMENT {experiment} — {len(rows)} runs over {len({r['ticket'] for r in rows})} tickets\n{'=' * 78}")
    print(f"  {'model':28} {'runs':>4} {'spend':>9} {'$/tkt':>8} {'turns':>6} {'assump':>7} {'grnd':>5} {'noSec':>6} {'tasks':>6} {'err':>4}")
    for model, rs in sorted(by_model.items(), key=lambda kv: sum(r["cost_usd"] for r in kv[1])):
        n = len(rs)
        spend = sum(r["cost_usd"] for r in rs)
        print(
            f"  {model:28.28} {n:>4} {spend:>8.4f}$ {spend / n:>7.4f}$ "
            f"{sum(r['turns'] for r in rs) / n:>6.1f} "
            f"{sum(r['assumptions'] for r in rs) / n:>7.1f} "
            f"{sum(r['grounded'] for r in rs) / n:>5.1f} "
            f"{sum(1 for r in rs if not r['has_section']):>6} "
            f"{sum(r['tasks_created'] for r in rs) / n:>6.1f} "
            f"{sum(1 for r in rs if r['error'] or r['degraded']):>4}"
        )

    print("\n  spend/turns are means per ticket. 'noSec' counts runs whose refined spec")
    print("  carried NO assumptions section at all — a contract violation, not a style")
    print("  difference, and the single clearest signal that a model is too weak here.")
    print("  'grnd' is a keyword heuristic for where to look, never a verdict.")

    degraded = [r for r in rows if r["degraded"] or r["error"]]
    if degraded:
        print("\n  -- runs that degraded --")
        for r in degraded:
            detail = r["error"] or ", ".join(r["degraded"])
            print(f"     {r['model']:28.28} {r['ticket']:22.22} {detail}")

    print("\n  Read the specs themselves before concluding. Identical assumption COUNTS")
    print("  can hide one model restating the ticket and another naming a real gap.")
    print(f"  Re-read later with:  uv run python scripts/e2e_matrix.py --report {experiment}")


async def report(experiment: str) -> int:
    """Re-print the scorecard for a past experiment from its recorded rows."""
    from minions.cli import _create_db

    grader = _load_grader()
    db = _create_db(Config.from_env())
    await db.connect()
    rows = []
    try:
        for job in await db.get_all_jobs():
            events = await db.get_events(job.id)
            marker = next((e for e in events if e["event_type"] == EXPERIMENT_EVENT), None)
            if marker is None:
                continue
            name, model, ticket = (marker.get("detail") or "||").split("|", 2)
            if name != experiment:
                continue
            agents = await db.get_agents_for_job(job.id)
            tasks = [t for t in await db.get_tasks(job.id) if t.service not in ("_spec", "_arbiter")]
            rows.append(
                _score(
                    {
                        "model": model,
                        "ticket": ticket,
                        "job_id": job.id,
                        "status": str(job.status),
                        "cost_usd": sum(a.cost_usd or 0.0 for a in agents),
                        "turns": sum(a.num_turns or 0 for a in agents),
                        "agents": len(agents),
                        "agents_failed": sum(1 for a in agents if a.status == "failed"),
                        "refined": job.original_spec is not None,
                        "spec": job.spec or "",
                        "tasks_created": len(tasks),
                        "events": {e["event_type"] for e in events},
                        "error": None,
                    },
                    grader,
                )
            )
    finally:
        await db.close()

    if not rows:
        print(f"No runs recorded for experiment {experiment!r}.", file=sys.stderr)
        return 2
    _print_report(rows, experiment)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare models over one ticket corpus.")
    parser.add_argument("--models", default="", help="Comma-separated model strings to compare")
    parser.add_argument("--tickets", default="all", help="Comma-separated ticket names, or 'all' (default)")
    parser.add_argument("--experiment", default="", help="Name for this run (default: derived from the models)")
    parser.add_argument("--mode", default="probe", choices=("probe", "live"), help="probe = analyst+arbiter only (default)")
    parser.add_argument("--report", default="", help="Re-print a past experiment's scorecard instead of running")
    args = parser.parse_args()

    if args.report:
        sys.exit(asyncio.run(report(args.report)))

    if not args.models:
        raise SystemExit("--models is required (e.g. --models claude-haiku-4-5,claude-sonnet-5)")
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    tickets = _resolve_tickets(args.tickets)
    experiment = args.experiment or "-".join(m.split("/")[-1][:12] for m in models)

    print(f"Experiment {experiment}: {len(models)} models x {len(tickets)} tickets = {len(models) * len(tickets)} real runs.")
    print("This costs tokens and writes job rows to whatever POSTGRES_URL points at.\n")
    sys.exit(asyncio.run(run_matrix(models, tickets, experiment, args.mode)))


if __name__ == "__main__":
    main()
