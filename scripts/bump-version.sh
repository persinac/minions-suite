#!/usr/bin/env bash
#
# Bump the patch version in VERSION and print the new version to stdout.
# Used by CI to auto-increment on each build.
#
# Mirrors flashback-cns/scripts/bump-version.sh so the two repos behave
# identically in a pipeline.
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
VERSION_FILE="$PROJECT_ROOT/VERSION"

if [[ ! -f "$VERSION_FILE" ]]; then
    echo "VERSION file not found" >&2
    exit 1
fi

CURRENT_VERSION=$(cat "$VERSION_FILE" | tr -d '[:space:]')

if [[ ! "$CURRENT_VERSION" =~ ^([0-9]+)\.([0-9]+)\.([0-9]+)$ ]]; then
    echo "Invalid version format: $CURRENT_VERSION (expected X.Y.Z)" >&2
    exit 1
fi

MAJOR="${BASH_REMATCH[1]}"
MINOR="${BASH_REMATCH[2]}"
PATCH="${BASH_REMATCH[3]}"

NEW_VERSION="${MAJOR}.${MINOR}.$((PATCH + 1))"

echo "$NEW_VERSION" > "$VERSION_FILE"
echo "$NEW_VERSION"
