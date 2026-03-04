#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["psycopg[binary]>=3.1", "nats-py>=2.9.0"]
# ///
"""
init-infra — idempotent infrastructure setup for minion-suite.

Run once after Postgres and NATS are up, before starting the MCP server.
Safe to re-run at any time; all operations check before acting.

Steps:
  [postgres] Create app role, grant schema/table privileges, set default privileges
  [nats]     Create REVIEWS JetStream stream and pre-create review-engine consumer

Usage:
    scripts/init-infra [options]

Options:
    --postgres-url URL    Admin Postgres URL (env: POSTGRES_ADMIN_URL, fallback: POSTGRES_URL)
    --nats-url URL        NATS server URL (env: NATS_SERVER_IP, default: nats://localhost:4222)
    --app-user NAME       DB role to create for the app (default: minion_app)
    --app-password PASS   Password for the app role (env: PG_APP_PASSWORD)
    --skip-postgres       Skip Postgres setup
    --skip-nats           Skip NATS setup

Environment:
    POSTGRES_ADMIN_URL   Admin connection string (superuser)
    POSTGRES_URL         Fallback if POSTGRES_ADMIN_URL not set
    PG_APP_PASSWORD      Password for the app role
    NATS_SERVER_IP       NATS server(s), comma-separated
"""

import argparse
import asyncio
import os
import sys

import nats
import psycopg
from psycopg import sql


# ── helpers ──────────────────────────────────────────────────────────────────


def _step(msg: str) -> None:
    print(f"    {msg}")


def _ok(msg: str) -> None:
    print(f"    [ok] {msg}")


def _section(title: str) -> None:
    print(f"\n[{title}]")


# ── PostgreSQL ────────────────────────────────────────────────────────────────

SCHEMA = "reviewer"


def setup_postgres(admin_url: str, app_user: str, app_password: str) -> None:
    _section("PostgreSQL")

    with psycopg.connect(admin_url, autocommit=True) as conn:
        db_name = conn.info.dbname

        # -- Role ----------------------------------------------------------
        exists = conn.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = %s", (app_user,)
        ).fetchone()

        if exists:
            _ok(f"role {app_user!r} already exists")
        else:
            _step(f"creating role {app_user!r} ...")
            conn.execute(
                sql.SQL("CREATE ROLE {} WITH LOGIN PASSWORD {}").format(
                    sql.Identifier(app_user),
                    sql.Literal(app_password),
                )
            )
            _ok(f"role {app_user!r} created")

        # -- Database CONNECT -----------------------------------------------
        conn.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(db_name),
                sql.Identifier(app_user),
            )
        )
        _ok(f"CONNECT on database {db_name!r} granted")

        # -- Schema --------------------------------------------------------
        conn.execute(
            sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(SCHEMA))
        )
        conn.execute(
            sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                sql.Identifier(SCHEMA),
                sql.Identifier(app_user),
            )
        )
        _ok(f"USAGE on schema {SCHEMA!r} granted")

        # -- Existing tables / sequences ------------------------------------
        conn.execute(
            sql.SQL(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {} TO {}"
            ).format(sql.Identifier(SCHEMA), sql.Identifier(app_user))
        )
        _ok("SELECT/INSERT/UPDATE/DELETE on existing tables granted")

        conn.execute(
            sql.SQL(
                "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {} TO {}"
            ).format(sql.Identifier(SCHEMA), sql.Identifier(app_user))
        )
        _ok("USAGE/SELECT on existing sequences granted")

        # -- Default privileges (future tables / sequences) ----------------
        conn.execute(
            sql.SQL(
                "ALTER DEFAULT PRIVILEGES IN SCHEMA {} "
                "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {}"
            ).format(sql.Identifier(SCHEMA), sql.Identifier(app_user))
        )
        conn.execute(
            sql.SQL(
                "ALTER DEFAULT PRIVILEGES IN SCHEMA {} "
                "GRANT USAGE, SELECT ON SEQUENCES TO {}"
            ).format(sql.Identifier(SCHEMA), sql.Identifier(app_user))
        )
        _ok("default privileges set (future tables/sequences will inherit)")


# ── NATS JetStream ────────────────────────────────────────────────────────────

STREAM_NAME = "REVIEWS"
STREAM_SUBJECTS = ["reviews.>"]
CONSUMER_NAME = "review-engine"
CONSUMER_FILTER = "reviews.requested.>"


async def setup_nats(nats_url: str) -> None:
    _section("NATS JetStream")

    nc = await nats.connect(nats_url)
    js = nc.jetstream()

    try:
        # -- Stream --------------------------------------------------------
        # add_stream is an upsert in NATS — safe to call on every run.
        _step(f"ensuring stream {STREAM_NAME!r} covering {STREAM_SUBJECTS} ...")
        await js.add_stream(name=STREAM_NAME, subjects=STREAM_SUBJECTS)
        info = await js.stream_info(STREAM_NAME)
        _ok(
            f"stream {STREAM_NAME!r} ready "
            f"(msgs={info.state.messages}, "
            f"storage={info.config.storage}, "
            f"subjects={info.config.subjects})"
        )

        # -- Durable consumer ----------------------------------------------
        # Pre-creating the consumer means the engine's position in the stream
        # is established before it ever starts — no messages are lost during
        # the window between stream creation and first engine connect.
        _step(f"ensuring durable consumer {CONSUMER_NAME!r} on {CONSUMER_FILTER!r} ...")
        try:
            cinfo = await js.consumer_info(STREAM_NAME, CONSUMER_NAME)
            _ok(
                f"consumer {CONSUMER_NAME!r} already exists "
                f"(pending={cinfo.num_pending}, "
                f"delivered={cinfo.delivered.stream_seq})"
            )
        except Exception:
            # Consumer doesn't exist yet; create it via a temporary subscribe/unsubscribe.
            # This is the idiomatic nats-py way to create a push consumer explicitly.
            sub = await js.subscribe(
                CONSUMER_FILTER,
                durable=CONSUMER_NAME,
                # deliver_policy="all" is the default — replay from beginning on first start
            )
            await sub.unsubscribe()
            _ok(f"consumer {CONSUMER_NAME!r} created (deliver_policy=all)")

    finally:
        await nc.drain()
        await nc.close()


# ── main ─────────────────────────────────────────────────────────────────────


def _normalize_nats_url(url: str) -> str:
    url = url.strip().split(",")[0].strip()  # take first server if comma-separated
    if not url.startswith(("nats://", "tls://", "ws://", "wss://")):
        url = f"nats://{url}"
    return url


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="init-infra",
        description="Idempotent infra setup: Postgres role/grants + NATS JetStream stream.",
    )
    parser.add_argument("--postgres-url", metavar="URL")
    parser.add_argument("--nats-url", metavar="URL")
    parser.add_argument("--app-user", metavar="NAME", default="minion_app")
    parser.add_argument("--app-password", metavar="PASS")
    parser.add_argument("--skip-postgres", action="store_true")
    parser.add_argument("--skip-nats", action="store_true")
    args = parser.parse_args()

    postgres_url = (
        args.postgres_url
        or os.environ.get("POSTGRES_ADMIN_URL")
        or os.environ.get("POSTGRES_URL", "")
    )
    nats_url = _normalize_nats_url(
        args.nats_url or os.environ.get("NATS_SERVER_IP", "nats://localhost:4222")
    )
    app_password = args.app_password or os.environ.get("PG_APP_PASSWORD", "")

    # Validate
    errors: list[str] = []
    if not args.skip_postgres:
        if not postgres_url:
            errors.append("Postgres URL required: --postgres-url / POSTGRES_ADMIN_URL / POSTGRES_URL")
        if not app_password:
            errors.append("App role password required: --app-password / PG_APP_PASSWORD")
    if errors:
        for e in errors:
            print(f"error: {e}", file=sys.stderr)
        sys.exit(1)

    # Redact password from URL for display
    display_pg = postgres_url
    if "@" in display_pg:
        display_pg = display_pg.split("@")[-1]

    print("init-infra")
    if not args.skip_postgres:
        print(f"  postgres : {display_pg}  (app role: {args.app_user!r})")
    if not args.skip_nats:
        print(f"  nats     : {nats_url}")

    if not args.skip_postgres:
        try:
            setup_postgres(postgres_url, args.app_user, app_password)
        except Exception as exc:
            print(f"\n[PostgreSQL] FAILED: {exc}", file=sys.stderr)
            sys.exit(1)

    if not args.skip_nats:
        try:
            asyncio.run(setup_nats(nats_url))
        except Exception as exc:
            print(f"\n[NATS] FAILED: {exc}", file=sys.stderr)
            sys.exit(1)

    print("\nDone. Infrastructure is ready.")
    if not args.skip_postgres:
        print(f"  Update POSTGRES_URL in .env to use role {args.app_user!r} before starting the server.")


if __name__ == "__main__":
    main()
