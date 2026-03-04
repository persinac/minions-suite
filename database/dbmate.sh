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

# If DATABASE_URL is already set, use it directly.
# Otherwise build it from individual DB_* env vars.
if [ -z "${DATABASE_URL:-}" ]; then
    for var in DB_ADMIN DB_PASSWORD DB_HOST DB_PORT DB_NAME; do
        if [ -z "${!var:-}" ]; then
            echo "Error: Neither DATABASE_URL nor $var is set."
            echo "Export DATABASE_URL, or set DB_ADMIN/DB_PASSWORD/DB_HOST/DB_PORT/DB_NAME."
            exit 1
        fi
    done

    # URL-encode user and password in case they contain special characters
    urlencode() {
        python3 -c "import urllib.parse; print(urllib.parse.quote('$1', safe=''))"
    }

    DB_USER_ENCODED=$(urlencode "$DB_ADMIN")
    DB_PASSWORD_ENCODED=$(urlencode "$DB_PASSWORD")

    DATABASE_URL="postgres://${DB_USER_ENCODED}:${DB_PASSWORD_ENCODED}@${DB_HOST}:${DB_PORT}/${DB_NAME}?sslmode=disable"
fi

export DATABASE_URL

# Run dbmate with migrations directory
dbmate --migrations-dir "$MIGRATIONS_DIR" --migrations-table "minions.schema_migrations" "$@"
