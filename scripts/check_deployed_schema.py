"""Refuse to release when production's schema is behind the code being shipped.

0.8.31 shipped `update_job_spec` writing `jobs.original_spec` and the deployed
database had no such column, because the migration had only ever been applied to
the local test container. Every new development job then died at the spec_ready
transition. It stayed invisible for forty minutes because no new job started in
that window, and cost $0.95 of relaunch loop to discover.

Nothing caught it. `task docker:release` builds, pushes and bumps the overlay; it
never looks at the database. The deploy verification that did run checked that
the new CODE was live in the pod, which was true, and never asked whether the
SCHEMA the code depends on was live, which was not.

This closes that. It compares the migration files in this checkout against
`minions.schema_migrations` in the deployed database and fails if any are
missing.

    uv run python scripts/check_deployed_schema.py          # gate: exit 1 if behind
    uv run python scripts/check_deployed_schema.py --list   # show both sides

The deployed database is firewalled to the cluster, so the query runs through
`kubectl exec` on the engine pod rather than connecting directly -- the same
route the engine itself uses, which is the point: if the pod cannot reach the
database, neither can the code being released.
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "database" / "pgsql" / "migrations"
NAMESPACE = "minion-suite"
VERSION_RE = re.compile(r"^(\d+)")

QUERY = """
import asyncio, json
from minions.config import Config
from minions.cli import _create_db
async def m():
    db = _create_db(Config.from_env()); await db.connect()
    async with db._pool.connection() as c:
        cur = await c.execute("SELECT version FROM minions.schema_migrations")
        rows = await cur.fetchall()
    out = [list(r.values())[0] if isinstance(r, dict) else r[0] for r in rows]
    print("APPLIED=" + json.dumps(sorted(str(v) for v in out)))
    await db.close()
asyncio.run(m())
"""


def versions_in(directory: Path) -> list[str]:
    """Migration versions in a directory, by filename prefix."""
    versions = []
    for path in sorted(directory.glob("*.sql")):
        match = VERSION_RE.match(path.name)
        if match:
            versions.append(match.group(1))
    return sorted(versions)


def local_versions() -> list[str]:
    return versions_in(MIGRATIONS_DIR)


def missing_versions(local: list[str], applied: list[str]) -> list[str]:
    """Migrations this checkout has that the deployed database does not.

    Deliberately one-directional. A deployed database AHEAD of the checkout is
    normal during a rollback and must not block a release; a database BEHIND is
    the case that ships code referencing columns that do not exist.
    """
    return [v for v in local if v not in set(applied)]


def engine_pod() -> str:
    result = subprocess.run(
        ["kubectl", "get", "pods", "-n", NAMESPACE, "--no-headers", "--field-selector=status.phase=Running", "-o", "custom-columns=:metadata.name"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    pods = [p for p in result.stdout.split() if p.startswith("minion-suite-")]
    if not pods:
        raise RuntimeError("no running minion-suite pod found")
    return pods[0]


def applied_versions() -> list[str]:
    result = subprocess.run(
        ["kubectl", "exec", "-i", "-n", NAMESPACE, engine_pod(), "--", "python", "-c", QUERY],
        capture_output=True,
        text=True,
        timeout=180,
    )
    for line in result.stdout.splitlines():
        if line.startswith("APPLIED="):
            return json.loads(line[len("APPLIED=") :])
    raise RuntimeError(f"could not read schema_migrations: {result.stderr.strip()[-300:]}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Fail if the deployed schema is behind this checkout.")
    parser.add_argument("--list", action="store_true", help="print both sides and exit 0")
    args = parser.parse_args()

    local = local_versions()
    try:
        applied = applied_versions()
    except Exception as exc:
        # Deliberately fatal rather than a warning. "I could not check" and
        # "everything is fine" must not look the same at a release gate -- that
        # equivalence is what let the original bug through.
        print(f"FAILED to read deployed schema: {exc}", file=sys.stderr)
        print("Refusing to vouch for a release we could not verify.", file=sys.stderr)
        sys.exit(2)

    missing = missing_versions(local, applied)

    if args.list:
        print(f"local ({len(local)}):   {local}")
        print(f"applied ({len(applied)}): {applied}")
        print(f"missing: {missing or 'none'}")
        sys.exit(0)

    if missing:
        print("DEPLOYED SCHEMA IS BEHIND THIS CHECKOUT", file=sys.stderr)
        print(f"  unapplied migrations: {missing}", file=sys.stderr)
        print("", file=sys.stderr)
        print("  Releasing now ships code whose columns do not exist yet. Apply them first:", file=sys.stderr)
        print("    DATABASE_URL=<deployed> database/dbmate.sh pgsql up", file=sys.stderr)
        print("", file=sys.stderr)
        print("  Override for a release that genuinely does not depend on them:", file=sys.stderr)
        print("    SKIP_SCHEMA_GATE=1 task docker:release", file=sys.stderr)
        sys.exit(1)

    print(f"deployed schema is current ({len(applied)} migrations applied)")


if __name__ == "__main__":
    main()
