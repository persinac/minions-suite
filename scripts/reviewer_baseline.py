#!/usr/bin/env python3
"""Snapshot the reviewer economics: cost, latency, and the quality gate.

Exists so a change to WHERE or HOW reviewers run (routing them to a cheaper
model, fanning them out to a herdr worker, changing the fanout width) can be
judged against a real before, not a remembered one. Run it, keep the output,
change the thing, run it again.

The quality half is not optional. Reviewer spend is easy to cut and the saving
is immediately visible; a weakened review gate is invisible until something bad
merges. So every section that reports a cost also reports what that cost was
buying -- verdict mix, revision rounds, comment volume. A change that halves
cost and moves `approve` from 51% to 80% is a regression wearing a saving's
clothes.

Usage:
    POSTGRES_URL=... uv run python scripts/reviewer_baseline.py
    # or, against the deployed DB, from a pod that already has the env:
    kubectl exec -n minion-suite deploy/input-sources -c input-sources -- \
        python3 /app/scripts/reviewer_baseline.py
"""

import asyncio
import sys
from datetime import UTC, datetime

from minions.config import Config
from minions.db.postgres import JOB_SCHEMA, PostgresDatabase

REVIEWER_ROLE = "code_reviewer"


def _pct(part: float, whole: float) -> float:
    if whole <= 0:
        return 0.0
    return part * 100.0 / whole


async def _by_model(conn) -> None:
    """Cost/latency per reviewer run, split by model.

    Split by model because that is the cheapest lever available: the same role
    running on two different models is the same work at two different prices,
    and the gap is knowable before any infrastructure changes.
    """
    print("== A. reviewer agents: cost / turns / cache / wall-clock, by model ==")
    cur = await conn.execute(
        f"""
        SELECT model, COUNT(*) n,
               AVG(cost_usd) avg_cost, SUM(cost_usd) tot_cost,
               AVG(num_turns) avg_turns,
               AVG(input_tokens) avg_in, AVG(cache_read_tokens) avg_cache,
               AVG(EXTRACT(EPOCH FROM (finished_at - started_at))) avg_secs
        FROM {JOB_SCHEMA}.agents
        WHERE role = %s AND finished_at IS NOT NULL
        GROUP BY model ORDER BY tot_cost DESC
        """,
        (REVIEWER_ROLE,),
    )
    rows = await cur.fetchall()
    grand = sum(float(r["tot_cost"] or 0) for r in rows)
    for r in rows:
        avg_in = float(r["avg_in"] or 0)
        avg_cache = float(r["avg_cache"] or 0)
        cache_rate = _pct(avg_cache, avg_in + avg_cache)
        tot = float(r["tot_cost"] or 0)
        print(
            f"  {str(r['model'])[:30]:30} n={r['n']:4} avg=${float(r['avg_cost'] or 0):6.3f} "
            f"tot=${tot:7.2f} ({_pct(tot, grand):4.1f}%) turns={float(r['avg_turns'] or 0):4.1f} "
            f"cache={cache_rate:4.1f}% {float(r['avg_secs'] or 0):5.0f}s"
        )
    print(f"  {'TOTAL':30} {'':10} {'':13} tot=${grand:7.2f}")


async def _hosts(conn) -> None:
    """Where reviewers actually ran.

    Currently uniform, which is the point: this is the column that will show a
    herdr fan-out working (or silently not working) without anyone asserting it.
    """
    print("\n== B. reviewer runs by host ==")
    cur = await conn.execute(
        f"SELECT COALESCE(NULLIF(host, ''), '(unset)') h, COUNT(*) n, SUM(cost_usd) tot "
        f"FROM {JOB_SCHEMA}.agents WHERE role = %s GROUP BY h ORDER BY n DESC",
        (REVIEWER_ROLE,),
    )
    for r in await cur.fetchall():
        print(f"  {str(r['h'])[:40]:40} n={r['n']:4}  tot=${float(r['tot'] or 0):7.2f}")


async def _verdicts(conn) -> None:
    """The gate itself. A shift here is the thing worth catching."""
    print("\n== C. verdict distribution ==")
    cur = await conn.execute(
        f"SELECT COALESCE(NULLIF(verdict, ''), '(empty)') v, COUNT(*) n FROM {JOB_SCHEMA}.tasks WHERE agent_role = %s GROUP BY v ORDER BY n DESC",
        (REVIEWER_ROLE,),
    )
    rows = await cur.fetchall()
    tot = sum(r["n"] for r in rows)
    for r in rows:
        print(f"  {r['v']!s:20} {r['n']:4}  {_pct(r['n'], tot):5.1f}%")
    print(f"  {'TOTAL':20} {tot:4}")


async def _specialties(conn) -> None:
    print("\n== D. by specialty (reviewer lens) ==")
    cur = await conn.execute(
        f"""
        SELECT COALESCE(NULLIF(specialty, ''), '(none)') s, COUNT(*) n,
               AVG(comments_posted) avg_comments,
               SUM(CASE WHEN verdict = 'approve' THEN 1 ELSE 0 END) * 100.0 / COUNT(*) approve_pct
        FROM {JOB_SCHEMA}.tasks WHERE agent_role = %s GROUP BY s ORDER BY n DESC
        """,
        (REVIEWER_ROLE,),
    )
    for r in await cur.fetchall():
        print(f"  {r['s']!s:24} n={r['n']:4} avg_comments={float(r['avg_comments'] or 0):4.1f} approve={float(r['approve_pct'] or 0):5.1f}%")


async def _revisions(conn) -> None:
    """The cost tail. Rounds at the cap are jobs that burned the full budget
    and still did not land, so they are the population any throughput change
    multiplies first."""
    print("\n== E. revision rounds ==")
    cur = await conn.execute(f"SELECT revision_count rc, COUNT(*) n FROM {JOB_SCHEMA}.tasks WHERE revision_count IS NOT NULL GROUP BY rc ORDER BY rc")
    rows = await cur.fetchall()
    tot = sum(r["n"] for r in rows)
    for r in rows:
        print(f"  rounds={r['rc']}  tasks={r['n']:4}  {_pct(r['n'], tot):5.1f}%")
    revised = sum(r["n"] for r in rows if r["rc"] and r["rc"] > 0)
    print(f"  -> {revised}/{tot} ({_pct(revised, tot):.1f}%) of tasks needed at least one revision")


async def main() -> int:
    cfg = Config.from_env()
    db = PostgresDatabase(cfg.postgres_url)
    await db.connect()
    print(f"reviewer baseline @ {datetime.now(UTC).isoformat(timespec='seconds')}  schema={JOB_SCHEMA}\n")
    async with db._pool.connection() as conn:
        await _by_model(conn)
        await _hosts(conn)
        await _verdicts(conn)
        await _specialties(conn)
        await _revisions(conn)
    await db.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
