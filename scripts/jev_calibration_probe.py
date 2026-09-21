"""Run the six calibration tickets from classifier.py's docstring through real Jev.

Those six have known-correct tiers, so this is the cheapest available check on whether
the uncalibrated thresholds in classifier_jev.py are anywhere near right -- and the
first real latency measurement, since the vendor publishes no figure.

Ticket text is written as genuine ticket prose. It deliberately does NOT name a
difficulty: jaggedness §6 warns that text arguing for its own classification moves the
answer, so labelling them would measure label-reading, not judgement.

Never prints TYPESAFE_API_KEY. Doppler injects it; this only asserts it is set.
"""

import asyncio
import os
import statistics
import sys
import time

sys.path.insert(0, "/home/persinac/repos/.worktrees/personal_minions-suite--jevai")

from minions.classifier import EASY, HARD, MEDIUM
from minions.classifier_jev import score_difficulty_jev


class _Cfg:
    classifier_max_chars = 6000
    classifier_min_confidence = 0.5
    typesafe_api_key = os.environ.get("TYPESAFE_API_KEY", "")
    typesafe_model = ""
    typesafe_timeout = 60.0


# (label, expected tier per classifier.py's calibration table, ticket text)
TICKETS = [
    (
        "typo in a constant",
        EASY,
        "The retry constant is spelled MAX_RETReIS in src/worker/config.py. Rename it to MAX_RETRIES and update the two call sites in the same file.",
    ),
    (
        "bump a dependency",
        EASY,
        "Bump the redis client from 5.0.1 to 5.2.0 in pyproject.toml and refresh the "
        "lockfile. The changelog lists no breaking changes for our usage.",
    ),
    (
        "add a mirrored test",
        EASY,
        "tests/test_invoices.py covers the happy path for refund_invoice but there is no "
        "test for the already-refunded case. Add one mirroring the existing test right "
        "below it; the fixture it needs is already in the file.",
    ),
    (
        "new endpoint, clear pattern",
        MEDIUM,
        "Add a GET /v1/accounts/{id}/statements endpoint returning the paginated statement "
        "list for an account. Follow the same handler/serializer/route layout as the "
        "existing /v1/accounts/{id}/transactions endpoint.",
    ),
    (
        "wallet-api new fixture pattern",
        HARD,
        "Our wallet tests each construct their own ledger state by hand, so they drift and "
        "a shared setup change touches nine files. We want a reusable fixture layer for "
        "ledger state, but we do not have a pattern for this anywhere in the repo and it is "
        "not clear whether it should be factory functions, fixtures, or a builder. Working "
        "out the right shape is most of the job.",
    ),
    (
        "auth refactor across services",
        HARD,
        "Token validation is duplicated across the api, worker and admin services, and each "
        "has drifted. Consolidate it. Every service that authenticates a request is "
        "affected, sessions must keep working through the change, and we have not decided "
        "whether this becomes a shared library or a validation service.",
    ),
]


async def main():
    if not _Cfg.typesafe_api_key:
        print("TYPESAFE_API_KEY is not set in this environment — run under doppler run.")
        return 1

    print(f"key present: yes (length {len(_Cfg.typesafe_api_key)})\n")

    agreed = 0
    total_cost = 0.0
    latencies = []
    rows = []

    for label, expected, text in TICKETS:
        started = time.monotonic()
        difficulty, reason, telemetry = await score_difficulty_jev(text, _Cfg())
        elapsed = time.monotonic() - started
        latencies.append(elapsed)

        if not telemetry:
            print(f"[{label}] FAILED: {reason}")
            continue

        total_cost += telemetry["cost_usd"]
        match = difficulty == expected
        agreed += int(match)
        rows.append((label, expected, difficulty, telemetry, elapsed, match))

        s = telemetry["scores"]
        c = telemetry["confidences"]
        n = telemetry["normalized"]
        print(f"[{label}]")
        print(
            f"  expected {expected:6s} -> jev {difficulty!s:6s} {'OK' if match else 'MISS'}   {elapsed * 1000:.0f} ms  ${telemetry['cost_usd']:.6f}"
        )
        print(f"  effort {s['effort']:.2f}/3 (conf {c['effort']:.2f})   clarity {s['clarity']:.2f}/2 (conf {c['clarity']:.2f})")
        print(
            f"  blast  {s['blast_radius']:.2f}/2 (conf {c['blast_radius']:.2f})   consequence {s['consequence']:.2f}/2 (conf {c['consequence']:.2f})"
        )
        print(
            f"  normalized e={n['effort_n']:.2f} c={n['clarity_n']:.2f} b={n['blast_n']:.2f} q={n['consequence_n']:.2f}  stakes_guard={n['stakes_guard']}  gated={telemetry['gated']}"
        )
        print(f"  effort probabilities {telemetry['probabilities']['effort']}")
        print()

    print("=" * 72)
    print(f"agreement with the calibration table: {agreed}/{len(TICKETS)}")
    if latencies:
        print(f"latency: median {statistics.median(latencies) * 1000:.0f} ms, max {max(latencies) * 1000:.0f} ms")
    print(f"total cost for {len(TICKETS)} classifications: ${total_cost:.6f}")
    misses = [(label, exp, got) for label, exp, got, _, _, ok in rows if not ok]
    if misses:
        print("\nmisses (these are what the thresholds have to be fitted against):")
        for label, exp, got in misses:
            print(f"  {label}: expected {exp}, got {got}")
    return 0


raise SystemExit(asyncio.run(main()))
