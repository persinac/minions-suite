#!/usr/bin/env bash
set -euo pipefail

if [ $# -lt 2 ]; then
    echo "Usage: $0 <database> <dbmate-command> [args...]"
    echo "Example: $0 pgsql up"
    exit 1
fi

DATABASE="$1"
shift

# Get the directory where this script lives
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MIGRATIONS_DIR="$SCRIPT_DIR/$DATABASE/migrations"

if [ ! -d "$MIGRATIONS_DIR" ]; then
    echo "Error: Migrations directory not found: $MIGRATIONS_DIR"
    exit 1
fi

# Credential resolution, in order of preference:
#
#   1. DATABASE_URL  — dbmate's own convention, and what `task db:migrate` sets
#   2. POSTGRES_URL  — what the *application* uses everywhere else: config.py
#                      reads it, the minion-suite-db Secret sets it, and the
#                      Pulumi stack composes it. It was previously ignored here,
#                      so the one credential already present in a running pod was
#                      the one this script refused, and migrations had to be
#                      applied by hand.
#   3. DB_* parts    — assembled below, for a box that exports the pieces
#
if [ -z "${DATABASE_URL:-}" ] && [ -n "${POSTGRES_URL:-}" ]; then
    DATABASE_URL="$POSTGRES_URL"
fi

if [ -z "${DATABASE_URL:-}" ]; then
    for var in DB_ADMIN DB_PASSWORD DB_HOST DB_PORT DB_NAME; do
        if [ -z "${!var:-}" ]; then
            echo "Error: no database credentials found."
            echo "Set one of:"
            echo "  DATABASE_URL   (dbmate convention)"
            echo "  POSTGRES_URL   (what the app and the k8s Secret use)"
            echo "  DB_ADMIN + DB_PASSWORD + DB_HOST + DB_PORT + DB_NAME"
            echo "Missing: $var"
            exit 1
        fi
    done

    # URL-encode user and password in case they contain special characters
    urlencode() {
        python3 -c "import urllib.parse; print(urllib.parse.quote('$1', safe=''))"
    }

    DB_USER_ENCODED=$(urlencode "$DB_ADMIN")
    DB_PASSWORD_ENCODED=$(urlencode "$DB_PASSWORD")

    # Default kept at disable for the local docker-compose Postgres. Note that
    # config.py's _build_postgres_url assembles sslmode=require from these same
    # five variables — so pointing DB_* at DO works for the app and fails here
    # unless DB_SSLMODE is set. Prefer POSTGRES_URL against a managed instance;
    # it carries the right mode already.
    DATABASE_URL="postgres://${DB_USER_ENCODED}:${DB_PASSWORD_ENCODED}@${DB_HOST}:${DB_PORT}/${DB_NAME}?sslmode=${DB_SSLMODE:-disable}"
fi

export DATABASE_URL

# Run dbmate with migrations directory
dbmate --migrations-dir "$MIGRATIONS_DIR" --migrations-table "minions.schema_migrations" "$@"
