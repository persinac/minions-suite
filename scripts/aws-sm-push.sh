#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["boto3"]
# ///
"""
aws-sm-push — create or update an AWS Secrets Manager secret from a .env file.

Usage:
    aws-sm-push <secret-name> [env-file] [options]

Arguments:
    secret-name     AWS Secrets Manager secret name or ARN
    env-file        Path to .env file (default: .env)

Options:
    --replace       Replace the entire secret with only the keys in the env file.
                    Default behaviour is to merge (add/update keys, keep others).
    --region NAME   AWS region (overrides AWS_REGION / AWS_DEFAULT_REGION)
    --dry-run       Print what would be written without making any changes
    --yes           Skip confirmation prompt

Examples:
    aws-sm-push minion/prod
    aws-sm-push minion/prod .env.production --replace
    aws-sm-push minion/prod --dry-run
"""

import argparse
import json
import os
import re
import sys

import boto3
from botocore.exceptions import ClientError


# ---------------------------------------------------------------------------
# .env parser
# ---------------------------------------------------------------------------

_UNQUOTE = re.compile(r'^(["\'])(.*)\1$', re.DOTALL)


def parse_env_file(path: str) -> dict[str, str]:
    """Parse a .env file into a dict. Handles comments, blank lines, quoted
    values, and optional `export` prefix. Values containing `=` are handled
    correctly (only the first `=` is the delimiter)."""
    result: dict[str, str] = {}

    with open(path) as fh:
        for lineno, raw in enumerate(fh, 1):
            line = raw.strip()

            if not line or line.startswith("#"):
                continue

            # Strip optional `export ` prefix
            if line.startswith("export "):
                line = line[7:].lstrip()

            if "=" not in line:
                print(f"  warning: line {lineno} skipped (no '=' found): {raw.rstrip()}", file=sys.stderr)
                continue

            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()

            # Strip surrounding quotes and unescape inner quotes
            m = _UNQUOTE.match(value)
            if m:
                quote, inner = m.group(1), m.group(2)
                value = inner.replace(f"\\{quote}", quote)

            # Strip inline comments for unquoted values (# preceded by whitespace)
            elif " #" in value:
                value = value[: value.index(" #")].rstrip()

            if not key:
                print(f"  warning: line {lineno} skipped (empty key)", file=sys.stderr)
                continue

            result[key] = value

    return result


# ---------------------------------------------------------------------------
# AWS helpers
# ---------------------------------------------------------------------------


def get_existing_secret(client, secret_name: str) -> dict[str, str] | None:
    """Return the current secret value as a dict, or None if it doesn't exist."""
    try:
        resp = client.get_secret_value(SecretId=secret_name)
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ResourceNotFoundException":
            return None
        raise

    raw = resp.get("SecretString")
    if raw is None:
        print(f"error: {secret_name!r} exists but contains a binary secret — cannot merge.", file=sys.stderr)
        sys.exit(1)

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        print(f"error: {secret_name!r} exists but is not JSON — cannot merge. Use --replace to overwrite.", file=sys.stderr)
        sys.exit(1)


def write_secret(client, secret_name: str, payload: dict[str, str], exists: bool, dry_run: bool) -> None:
    secret_string = json.dumps(payload)

    if dry_run:
        return

    if exists:
        client.put_secret_value(SecretId=secret_name, SecretString=secret_string)
    else:
        client.create_secret(Name=secret_name, SecretString=secret_string)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="aws-sm-push",
        description="Create or update an AWS Secrets Manager secret from a .env file.",
        add_help=True,
    )
    parser.add_argument("secret_name", metavar="secret-name")
    parser.add_argument("env_file", metavar="env-file", nargs="?", default=".env")
    parser.add_argument("--replace", action="store_true", help="Replace entire secret (don't merge with existing keys)")
    parser.add_argument("--region", metavar="NAME", help="AWS region")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without writing")
    parser.add_argument("--yes", action="store_true", help="Skip confirmation prompt")
    args = parser.parse_args()

    # Validate env file
    if not os.path.isfile(args.env_file):
        print(f"error: env file not found: {args.env_file}", file=sys.stderr)
        sys.exit(1)

    incoming = parse_env_file(args.env_file)
    if not incoming:
        print(f"error: no key=value pairs found in {args.env_file}", file=sys.stderr)
        sys.exit(1)

    region = args.region or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    client = boto3.client("secretsmanager", **({"region_name": region} if region else {}))

    existing = get_existing_secret(client, args.secret_name)
    action = "update" if existing is not None else "create"

    # Build the final payload
    if args.replace or existing is None:
        final = dict(incoming)
    else:
        final = {**existing, **incoming}  # incoming keys win

    # ── Summary ─────────────────────────────────────────────────────────────
    tag = "[DRY RUN] " if args.dry_run else ""
    print(f"\n{tag}Secret : {args.secret_name}")
    print(f"{tag}Action : {action} ({'replace' if args.replace else 'merge'})")
    print(f"{tag}Source : {args.env_file}  ({len(incoming)} key(s))\n")

    if existing and not args.replace:
        added = [k for k in incoming if k not in existing]
        updated = [k for k in incoming if k in existing and existing[k] != incoming[k]]
        unchanged = [k for k in incoming if k in existing and existing[k] == incoming[k]]
        kept = [k for k in existing if k not in incoming]

        if added:
            print(f"  + adding   : {', '.join(sorted(added))}")
        if updated:
            print(f"  ~ updating : {', '.join(sorted(updated))}")
        if unchanged:
            print(f"  = unchanged: {', '.join(sorted(unchanged))}")
        if kept:
            print(f"  . keeping  : {', '.join(sorted(kept))}")
    else:
        print(f"  keys: {', '.join(sorted(final))}")

    print()

    if args.dry_run:
        print("Dry run — no changes made.")
        return

    if not args.yes:
        try:
            answer = input(f"Write {len(final)} key(s) to {args.secret_name!r}? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            sys.exit(0)
        if answer not in ("y", "yes"):
            print("Aborted.")
            sys.exit(0)

    write_secret(client, args.secret_name, final, exists=(existing is not None), dry_run=False)
    print(f"Done — secret {action}d: {args.secret_name}")


if __name__ == "__main__":
    main()
