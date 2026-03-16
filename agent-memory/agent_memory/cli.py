"""CLI for inspecting and debugging the agent-memory system.

Usage:
  python -m agent_memory.cli inspect [--tier l2|l3|all] [--project NAME]
  python -m agent_memory.cli inspect --graph [--project NAME]
  python -m agent_memory.cli stats [--project NAME]
  python -m agent_memory.cli trace --project NAME --query "search terms"
  python -m agent_memory.cli flush --tier l2 --project NAME
  python -m agent_memory.cli flush --all --project NAME
"""

import argparse
import asyncio
import logging
import os
import sys


def _redis_url() -> str:
    return os.getenv("REDIS_URL", "redis://localhost:6379")


def _redis_password() -> str | None:
    return os.getenv("REDIS_PASSWORD") or None


def _postgres_url() -> str:
    return os.getenv("POSTGRES_URL", "")


async def cmd_inspect(args):
    """Inspect memory system state."""
    tier = args.tier or "all"

    if tier in ("l2", "all"):
        await _inspect_l2(args.project)
    if tier in ("l3", "all"):
        await _inspect_l3(args.project)
    if args.graph:
        await _inspect_graph(args.project)


async def _inspect_l2(project: str | None):
    """Show all L2 facts in Redis."""
    from .backends.redis import RedisTupleSpaceBackend
    from .tuplespace import TupleSpace

    backend = RedisTupleSpaceBackend(url=_redis_url(), password=_redis_password())
    ts = TupleSpace(backend, project=project or "*")
    await ts.connect()

    print("\n═══ L2 TupleSpace (Redis) ═══\n")

    if project:
        facts = await ts.rd(limit=100)
        _print_facts(facts)
    else:
        # Scan all keys to discover projects
        keys = await backend.keys("fact:*")
        projects = set()
        for k in keys:
            parts = k.split(":")
            if len(parts) >= 2:
                projects.add(parts[1])

        if not projects:
            print("  (empty — no facts in L2)")
        else:
            for proj in sorted(projects):
                proj_ts = TupleSpace(backend, project=proj)
                facts = await proj_ts.rd(limit=100)
                print(f"  Project: {proj} ({len(facts)} facts)")
                _print_facts(facts, indent=4)
                print()

    await ts.close()


def _print_facts(facts, indent=2):
    pad = " " * indent
    if not facts:
        print(f"{pad}(no facts)")
        return
    for fact in facts:
        ttl_str = ""
        print(f"{pad}[{fact.category}] {fact.key} = {fact.value[:80]}{ttl_str}")
        meta_parts = []
        if fact.tags:
            meta_parts.append(f"tags={fact.tags}")
        if fact.agent_role:
            meta_parts.append(f"role={fact.agent_role}")
        if fact.job_id:
            meta_parts.append(f"job={fact.job_id}")
        if meta_parts:
            print(f"{pad}  {', '.join(meta_parts)}")


async def _inspect_l3(project: str | None):
    """Show L3 knowledge graph contents in Postgres."""
    pg_url = _postgres_url()
    if not pg_url:
        print("\n═══ L3 Knowledge Graph (Postgres) ═══\n")
        print("  POSTGRES_URL not set — skipping L3 inspection")
        return

    try:
        import psycopg
    except ImportError:
        print("  psycopg not installed — skipping L3 inspection")
        return

    print("\n═══ L3 Knowledge Graph (Postgres) ═══\n")

    with psycopg.connect(pg_url) as conn, conn.cursor() as cur:
        # Nodes
        where = "WHERE project = %s" if project else ""
        params = (project,) if project else ()

        cur.execute(f"SELECT count(*) FROM minions.memory_nodes {where}", params)
        node_count = cur.fetchone()[0]

        cur.execute(f"SELECT count(*) FROM minions.memory_entities {where}", params)
        entity_count = cur.fetchone()[0]

        cur.execute(
            f"""SELECT count(*) FROM minions.memory_links lk
                JOIN minions.memory_nodes n ON lk.from_node = n.id
                {"WHERE n.project = %s" if project else ""}""",
            params,
        )
        link_count = cur.fetchone()[0]

        # pgvector stats
        cur.execute(
            f"SELECT count(*) FROM minions.memory_nodes {where} AND embedding IS NOT NULL"
            if project
            else "SELECT count(*) FROM minions.memory_nodes WHERE embedding IS NOT NULL"
        )
        vector_count = cur.fetchone()[0]

        print(f"  Nodes:      {node_count}")
        print(f"  Entities:   {entity_count}")
        print(f"  Links:      {link_count}")
        print(f"  Vectors:    {vector_count} (nodes with embeddings)")

        # Recent nodes
        cur.execute(
            f"""SELECT id, title, tags, source_agent_role, access_count, created_at,
                       (embedding IS NOT NULL) as has_vector
                FROM minions.memory_nodes {where}
                ORDER BY created_at DESC LIMIT 10""",
            params,
        )
        rows = cur.fetchall()
        if rows:
            print("\n  Recent nodes:")
            for row in rows:
                nid, title, tags, role, access, _created, has_vec = row
                vec_marker = " [VEC]" if has_vec else ""
                print(f"    {nid[:10]}  {title or '(untitled)':40s}  tags={tags}  access={access}  role={role}{vec_marker}")

        # Top entities by backlink count
        cur.execute(
            f"""SELECT lk.to_entity, count(*) as cnt
                FROM minions.memory_links lk
                JOIN minions.memory_nodes n ON lk.from_node = n.id
                {"WHERE n.project = %s" if project else ""}
                GROUP BY lk.to_entity ORDER BY cnt DESC LIMIT 10""",
            params,
        )
        entities = cur.fetchall()
        if entities:
            print("\n  Top entities (by backlink count):")
            for ent_name, cnt in entities:
                print(f"    {ent_name:40s}  {cnt} backlinks")

        # Link type distribution
        cur.execute(
            f"""SELECT lk.link_type, count(*) as cnt
                FROM minions.memory_links lk
                JOIN minions.memory_nodes n ON lk.from_node = n.id
                {"WHERE n.project = %s" if project else ""}
                GROUP BY lk.link_type ORDER BY cnt DESC""",
            params,
        )
        link_types = cur.fetchall()
        if link_types:
            print("\n  Link types:")
            for lt, cnt in link_types:
                print(f"    {lt:20s}  {cnt}")


async def _inspect_graph(project: str | None):
    """Show AGE graph structure if available."""
    pg_url = _postgres_url()
    if not pg_url:
        print("\n═══ AGE Graph ═══\n  POSTGRES_URL not set")
        return

    print("\n═══ AGE Graph (Cypher) ═══\n")
    try:
        import psycopg

        with psycopg.connect(pg_url) as conn, conn.cursor() as cur:
            # Try AGE query
            cur.execute("LOAD 'age'")
            cur.execute('SET search_path = ag_catalog, "$user", public')
            cur.execute("SELECT * FROM cypher('knowledge', $$ MATCH (n) RETURN count(n) $$) AS (cnt agtype)")
            row = cur.fetchone()
            print(f"  Graph nodes: {row[0] if row else 0}")

            cur.execute("SELECT * FROM cypher('knowledge', $$ MATCH ()-[r]->() RETURN count(r) $$) AS (cnt agtype)")
            row = cur.fetchone()
            print(f"  Graph edges: {row[0] if row else 0}")
    except Exception as e:
        print(f"  AGE not available or query failed: {e}")
        print("  (Graph data is also stored in memory_links table — use 'inspect --tier l3' to see it)")


async def cmd_stats(args):
    """Show memory system statistics."""
    print("\n═══ Memory System Stats ═══\n")

    # L2 stats
    try:
        from .backends.redis import RedisTupleSpaceBackend

        backend = RedisTupleSpaceBackend(url=_redis_url(), password=_redis_password())
        await backend.connect()
        keys = await backend.keys("fact:*")

        projects: dict[str, int] = {}
        for k in keys:
            parts = k.split(":")
            if len(parts) >= 2:
                proj = parts[1]
                projects[proj] = projects.get(proj, 0) + 1

        print("  L2 (Redis):")
        print(f"    Total facts: {len(keys)}")
        for proj, cnt in sorted(projects.items()):
            print(f"    {proj}: {cnt} facts")
        if not projects:
            print("    (empty)")
        await backend.close()
    except Exception as e:
        print(f"  L2 (Redis): unavailable — {e}")

    # L3 stats
    pg_url = _postgres_url()
    if pg_url:
        try:
            import psycopg

            with psycopg.connect(pg_url) as conn, conn.cursor() as cur:
                cur.execute("SELECT project, count(*) FROM minions.memory_nodes GROUP BY project ORDER BY count(*) DESC")
                rows = cur.fetchall()
                print("\n  L3 (Postgres):")
                if rows:
                    for proj, cnt in rows:
                        cur.execute("SELECT count(*) FROM minions.memory_nodes WHERE project = %s AND embedding IS NOT NULL", (proj,))
                        vec_count = cur.fetchone()[0]
                        print(f"    {proj}: {cnt} nodes ({vec_count} with vectors)")
                else:
                    print("    (empty)")
        except Exception as e:
            print(f"\n  L3 (Postgres): unavailable — {e}")
    else:
        print("\n  L3 (Postgres): POSTGRES_URL not set")


async def cmd_trace(args):
    """Trace a memory retrieval to see scoring details."""

    # Enable DEBUG tracing for memory
    logging.basicConfig(level=logging.DEBUG, format="%(name)s %(message)s")
    trace_logger = logging.getLogger("agent_memory.trace")
    trace_logger.setLevel(logging.DEBUG)

    print("\n═══ Memory Retrieval Trace ═══")
    print(f"  Project: {args.project}")
    print(f"  Query: {args.query}\n")

    pg_url = _postgres_url()
    if not pg_url:
        print("  POSTGRES_URL not set — cannot trace L3 retrieval")
        return

    from .backends.postgres import PostgresMemoryBackend
    from .context import build_knowledge_context
    from .store import MemoryStore

    backend = PostgresMemoryBackend(conninfo=pg_url)
    store = MemoryStore(backend)
    await store.connect()

    result = await build_knowledge_context(store, args.project, task_description=args.query, max_tokens=args.budget)

    print("\n  ── Result ──")
    if result:
        print(f"  {len(result)} chars of context generated")
        print(f"\n{result[:500]}...")
    else:
        print("  (empty — no relevant memories found)")

    await store.close()


async def cmd_flush(args):
    """Flush memory for a project."""
    if not args.project:
        print("Error: --project is required for flush")
        sys.exit(1)

    confirm = input(f"Flush {'ALL' if args.all else args.tier} memory for project '{args.project}'? [y/N] ")
    if confirm.lower() != "y":
        print("Aborted.")
        return

    if args.tier == "l2" or args.all:
        from .backends.redis import RedisTupleSpaceBackend
        from .tuplespace import TupleSpace

        backend = RedisTupleSpaceBackend(url=_redis_url(), password=_redis_password())
        ts = TupleSpace(backend, project=args.project)
        await ts.connect()
        removed = await ts.expire_project()
        print(f"  L2: removed {removed} facts")
        await ts.close()

    if args.tier == "l3" or args.all:
        pg_url = _postgres_url()
        if pg_url:
            import psycopg

            with psycopg.connect(pg_url) as conn, conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM minions.memory_links WHERE from_node IN (SELECT id FROM minions.memory_nodes WHERE project = %s)", (args.project,)
                )
                cur.execute("DELETE FROM minions.memory_nodes WHERE project = %s", (args.project,))
                nodes = cur.rowcount
                cur.execute("DELETE FROM minions.memory_entities WHERE project = %s", (args.project,))
                entities = cur.rowcount
                conn.commit()
                print(f"  L3: removed {nodes} nodes, {entities} entities")


def main():
    parser = argparse.ArgumentParser(description="agent-memory inspection CLI")
    sub = parser.add_subparsers(dest="command")

    # inspect
    p_inspect = sub.add_parser("inspect", help="Inspect memory system state")
    p_inspect.add_argument("--tier", choices=["l2", "l3", "all"], default="all")
    p_inspect.add_argument("--project", default=None)
    p_inspect.add_argument("--graph", action="store_true")

    # stats
    p_stats = sub.add_parser("stats", help="Show memory statistics")
    p_stats.add_argument("--project", default=None)

    # trace
    p_trace = sub.add_parser("trace", help="Trace a memory retrieval")
    p_trace.add_argument("--project", required=True)
    p_trace.add_argument("--query", required=True)
    p_trace.add_argument("--budget", type=int, default=2000)

    # flush
    p_flush = sub.add_parser("flush", help="Flush memory for a project")
    p_flush.add_argument("--tier", choices=["l2", "l3"], default="l2")
    p_flush.add_argument("--all", action="store_true")
    p_flush.add_argument("--project", required=True)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    handlers = {
        "inspect": cmd_inspect,
        "stats": cmd_stats,
        "trace": cmd_trace,
        "flush": cmd_flush,
    }
    asyncio.run(handlers[args.command](args))


if __name__ == "__main__":
    main()
