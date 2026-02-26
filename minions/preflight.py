"""Preflight checks for the PR reviewer."""

import asyncio
import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Optional

from .config import Config

logger = logging.getLogger(__name__)

PASS = "[PASS]"
FAIL = "[FAIL]"
WARN = "[WARN]"


@dataclass
class Check:
    name: str
    status: str
    detail: str
    required: bool = True


def _run(cmd: list[str], timeout: int = 10) -> tuple[int, str, str]:
    """Run a command and return (returncode, stdout, stderr)."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace")
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except FileNotFoundError:
        return -1, "", f"{cmd[0]}: command not found"
    except subprocess.TimeoutExpired:
        return -1, "", f"{cmd[0]}: timed out after {timeout}s"
    except Exception as e:
        return -1, "", str(e)


def check_cli(name: str, version_cmd: list[str], required: bool = True) -> Check:
    """Check if a CLI tool is installed and get its version."""
    path = shutil.which(name)
    if not path:
        return Check(name, FAIL if required else WARN, "not found on PATH", required=required)

    code, stdout, stderr = _run(version_cmd)
    version = (stdout or stderr).split("\n")[0][:80]
    if code == 0:
        return Check(name, PASS, version, required=required)
    if version:
        return Check(name, PASS, version, required=required)
    return Check(name, FAIL if required else WARN, f"found at {path} but version check failed", required=required)


def check_litellm() -> Check:
    """Check if LiteLLM is importable and a model key is configured."""
    try:
        import litellm
        version = getattr(litellm, "__version__", "unknown")
    except ImportError:
        return Check("litellm", FAIL, "not installed -- run: uv add litellm")

    # Check for at least one API key
    has_key = any([
        os.getenv("OPENAI_API_KEY"),
        os.getenv("ANTHROPIC_API_KEY"),
        os.getenv("AZURE_API_KEY"),
        os.getenv("GEMINI_API_KEY"),
    ])
    if has_key:
        return Check("litellm", PASS, f"v{version}, API key found")
    return Check("litellm", WARN, f"v{version}, no API key found in env (OPENAI_API_KEY, ANTHROPIC_API_KEY, etc.)")


def check_git_provider(config: Config) -> Check:
    """Check git provider credentials."""
    if config.git_provider == "gitlab":
        if config.gitlab_token:
            return Check("gitlab auth", PASS, f"token set, url={config.gitlab_url or 'not set'}")
        return Check("gitlab auth", FAIL, "GITLAB_TOKEN not set")

    if config.git_provider == "github":
        if config.github_token:
            return Check("github auth", PASS, "GH_TOKEN set")
        # Check gh CLI auth
        code, stdout, stderr = _run(["gh", "auth", "status"])
        if code == 0:
            return Check("github auth", PASS, "gh CLI authenticated")
        return Check("github auth", FAIL, "GH_TOKEN not set and gh CLI not authenticated")

    return Check("git provider", WARN, f"unknown provider: {config.git_provider}")


def check_postgres(config: Config) -> Check:
    """Check if Postgres is reachable."""
    if config.db_backend != "postgres":
        return Check("postgres", WARN, f"not required (db_backend={config.db_backend})", required=False)
    if not config.postgres_url:
        return Check("postgres", FAIL, "POSTGRES_URL not set")
    try:
        import psycopg

        with psycopg.connect(config.postgres_url, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                return Check("postgres", PASS, "connected")
    except ImportError:
        return Check("postgres", FAIL, "psycopg not installed -- run: uv add psycopg[binary]")
    except Exception as e:
        return Check("postgres", FAIL, f"connection failed: {str(e)[:80]}")


def check_nats(config: Config) -> Check:
    """Check if NATS is reachable."""
    if not config.nats_enabled:
        return Check("nats", WARN, "disabled (NATS_ENABLED not set)", required=False)
    try:
        import nats

        from .connectors.nats_config import NatsConfig

        nats_config = NatsConfig.from_env()

        async def _probe():
            connect_opts = {"servers": nats_config.servers}
            if nats_config.user and nats_config.password:
                connect_opts["user"] = nats_config.user
                connect_opts["password"] = nats_config.password
            nc = await nats.connect(**connect_opts)
            await nc.close()

        try:
            asyncio.get_running_loop()
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(asyncio.run, _probe()).result(timeout=10)
        except RuntimeError:
            asyncio.run(_probe())

        servers_str = ", ".join(nats_config.servers)
        return Check("nats", PASS, f"connected to {servers_str}")
    except ImportError:
        return Check("nats", FAIL, "nats-py not installed -- run: uv add nats-py")
    except Exception as e:
        return Check("nats", FAIL, f"connection failed: {str(e)[:80]}")


def run_preflight(config: Optional[Config] = None) -> list[Check]:
    """Run all preflight checks and return results."""
    config = config or Config.from_env()
    checks: list[Check] = []

    # Core CLIs
    checks.append(check_cli("git", ["git", "--version"]))
    checks.append(check_cli("rg", ["rg", "--version"], required=False))

    # LiteLLM
    checks.append(check_litellm())

    # Git provider
    checks.append(check_git_provider(config))

    # Database
    if config.db_backend == "postgres":
        checks.append(check_postgres(config))

    # NATS
    if config.nats_enabled:
        checks.append(check_nats(config))

    return checks


def print_preflight(checks: list[Check]) -> bool:
    """Print preflight results and return True if all required checks pass."""
    print("\n=== PR Reviewer Preflight Checks ===\n")

    max_name = max(len(c.name) for c in checks)
    failed_required = False

    for c in checks:
        padding = " " * (max_name - len(c.name) + 2)
        print(f"  {c.status} {c.name}{padding}{c.detail}")
        if c.status == FAIL and c.required:
            failed_required = True

    passed = sum(1 for c in checks if c.status == PASS)
    warned = sum(1 for c in checks if c.status == WARN)
    failed = sum(1 for c in checks if c.status == FAIL)

    print(f"\n  {passed} passed, {warned} warnings, {failed} failed")

    if failed_required:
        print("\n  Required checks failed. Fix the above issues before starting.\n")
        return False
    if warned:
        print("\n  All required checks passed. Warnings are for optional features.\n")
        return True
    print("\n  All checks passed. Ready to review.\n")
    return True
