#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["boto3"]
# ///
"""
aws-sm-run — fetch secrets from AWS Secrets Manager and exec a command,
drop-in replacement for `doppler run --`.

Usage:
    aws-sm-run <secret-name> [<secret-name>...] -- <command> [args...]

Examples:
    aws-sm-run minion/prod -- uv run python -m minions --server
    aws-sm-run minion/prod minion/extra -- uv run python -m minions review <url>

Each secret must be stored as a flat JSON object of string key-value pairs.
AWS credentials are resolved via the standard boto3 chain (env vars,
~/.aws/credentials, IAM instance role, ECS task role, etc.).

Set AWS_REGION or AWS_DEFAULT_REGION to target a specific region.
"""

import json
import os
import sys

import boto3
from botocore.exceptions import ClientError


def fetch_secret(client, secret_name: str) -> dict[str, str]:
    try:
        resp = client.get_secret_value(SecretId=secret_name)
    except ClientError as exc:
        print(f"aws-sm-run: failed to fetch {secret_name!r}: {exc}", file=sys.stderr)
        sys.exit(1)

    raw = resp.get("SecretString")
    if raw is None:
        print(f"aws-sm-run: {secret_name!r} has no SecretString (binary secrets not supported)", file=sys.stderr)
        sys.exit(1)

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"aws-sm-run: {secret_name!r} is not valid JSON: {exc}", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    args = sys.argv[1:]

    try:
        sep = args.index("--")
    except ValueError:
        print("Usage: aws-sm-run <secret-name> [<secret-name>...] -- <command> [args...]", file=sys.stderr)
        sys.exit(1)

    secret_names = args[:sep]
    command = args[sep + 1:]

    if not secret_names:
        print("aws-sm-run: at least one secret name is required before --", file=sys.stderr)
        sys.exit(1)

    if not command:
        print("aws-sm-run: no command specified after --", file=sys.stderr)
        sys.exit(1)

    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    client = boto3.client("secretsmanager", **({"region_name": region} if region else {}))

    env = os.environ.copy()
    for name in secret_names:
        secrets = fetch_secret(client, name)
        env.update({k: str(v) for k, v in secrets.items()})

    # Replace this process — identical behaviour to `doppler run --`
    os.execvpe(command[0], command, env)


if __name__ == "__main__":
    main()
