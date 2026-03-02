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

# Construct DATABASE_URL from environment variables or Doppler secrets
DB_USER="${DB_ADMIN:-$(doppler secrets get DB_ADMIN --plain --project mcp-minions --config prd)}"
DB_PASSWORD="${DB_PASSWORD:-$(doppler secrets get DB_PASSWORD --plain --project mcp-minions --config prd)}"
DB_HOST="${DB_HOST:-$(doppler secrets get DB_HOST --plain --project mcp-minions --config prd)}"
DB_PORT="${DB_PORT:-$(doppler secrets get DB_PORT --plain --project mcp-minions --config prd)}"
DB_NAME="${DB_NAME:-$(doppler secrets get DB_NAME --plain --project mcp-minions --config prd)}"

# URL-encode user and password in case they contain special characters
urlencode() {
    python3 -c "import urllib.parse; print(urllib.parse.quote('$1', safe=''))"
}

DB_USER_ENCODED=$(urlencode "$DB_USER")
DB_PASSWORD_ENCODED=$(urlencode "$DB_PASSWORD")

DATABASE_URL="postgres://${DB_USER_ENCODED}:${DB_PASSWORD_ENCODED}@${DB_HOST}:${DB_PORT}/${DB_NAME}?sslmode=disable"

export DATABASE_URL

# Run dbmate with migrations directory
dbmate --migrations-dir "$MIGRATIONS_DIR" --migrations-table "minions.schema_migrations" "$@"
