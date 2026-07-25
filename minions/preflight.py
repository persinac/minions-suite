"""Preflight checks for the minions-suite."""

import asyncio
import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse

from .config import Config

logger = logging.getLogger(__name__)

PASS = "[PASS]"
FAIL = "[FAIL]"
WARN = "[WARN]"


def _in_container() -> bool:
    """Detect if running inside a Docker/K8s container."""
    return os.getenv("CONTAINER_ENV", "").lower() in ("1", "true", "yes") or os.path.exists("/.dockerenv")


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
    has_key = any(
        [
            os.getenv("OPENAI_API_KEY"),
            os.getenv("ANTHROPIC_API_KEY"),
            os.getenv("AZURE_API_KEY"),
            os.getenv("GEMINI_API_KEY"),
        ]
    )
    if has_key:
        return Check("litellm", PASS, f"v{version}, API key found")
    return Check("litellm", WARN, f"v{version}, no API key found in env (OPENAI_API_KEY, ANTHROPIC_API_KEY, etc.)")


def check_git_provider(config: Config) -> Check:
    """Check git provider credentials."""
    container = _in_container()

    if config.git_provider == "gitlab":
        if config.gitlab_token:
            return Check("gitlab auth", PASS, f"token set, url={config.gitlab_url or 'not set'}")
        return Check("gitlab auth", FAIL if not container else WARN, "GITLAB_TOKEN not set", required=not container)

    if config.git_provider == "github":
        # GitHub App first — it supersedes a static PAT when configured.
        # Actually mint a token rather than checking the vars are non-empty: a
        # mismatched App ID, a mangled PEM, or an uninstalled App all look fine
        # by presence and fail at the first `gh` call, hours into a job.
        if config.github_app_id and config.github_app_private_key and config.github_app_installation_id:
            from .providers.github_app import GitHubAppError, build_token_provider

            try:
                provider = build_token_provider(config)
                asyncio.run(provider.token())
                return Check("github auth", PASS, f"GitHub App {config.github_app_id} installation {config.github_app_installation_id}")
            except (GitHubAppError, ValueError) as e:
                return Check("github auth", FAIL, f"GitHub App: {e}")

        if config.github_token:
            return Check("github auth", PASS, "GH_TOKEN set")
        # Check gh CLI auth
        code, _stdout, _stderr = _run(["gh", "auth", "status"])
        if code == 0:
            return Check("github auth", PASS, "gh CLI authenticated")
        hint = "set GH_TOKEN env var" if container else "run: gh auth login"
        return Check("github auth", FAIL, f"not authenticated - {hint}")

    return Check("git provider", WARN, f"unknown provider: {config.git_provider}")


def check_doppler_auth() -> Check:
    """Check if doppler is authenticated."""
    code, stdout, _stderr = _run(["doppler", "me"])
    if code == 0:
        for line in stdout.split("\n"):
            if "Workplace" in line or "Email" in line:
                return Check("doppler auth", PASS, line.strip())
        return Check("doppler auth", PASS, "authenticated")
    return Check("doppler auth", FAIL, "not authenticated - run: doppler login")


def _aws_profile_args(profile: str) -> list[str]:
    """Return ['--profile', profile] unless env-based AWS credentials are set."""
    if os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY"):
        return []
    return ["--profile", profile]


def check_aws_auth(profile: str = "mcp-minions") -> Check:
    """Check if AWS CLI is authenticated via env vars or named profile."""
    profile_args = _aws_profile_args(profile)
    cmd = ["aws", "sts", "get-caller-identity", *profile_args]
    code, _stdout, stderr = _run(cmd)
    if code == 0:
        source = "env credentials" if not profile_args else f"profile: {profile}"
        return Check("aws auth", PASS, f"{source}, authenticated", required=False)
    if "could not be found" in stderr.lower() or "not found" in stderr.lower():
        return Check(
            "aws auth",
            WARN,
            f"profile '{profile}' not found in ~/.aws/credentials - add static IAM keys for the mcp-minions user",
            required=False,
        )
    return Check("aws auth", WARN, f"auth failed: {stderr[:80]}", required=False)


def check_apprunner_access(profile: str = "mcp-minions") -> Check:
    """Check if AWS CLI can list AppRunner services."""
    cmd = ["aws", "apprunner", "list-services", *_aws_profile_args(profile), "--max-results", "1"]
    code, _stdout, stderr = _run(cmd)
    if code == 0:
        source = "env credentials" if not _aws_profile_args(profile) else f"profile: {profile}"
        return Check("apprunner access", PASS, f"can list AppRunner services ({source})", required=False)
    if "ExpiredToken" in stderr or "SSO" in stderr or "expired" in stderr.lower():
        return Check("apprunner access", WARN, f"session expired: {stderr[:60]}", required=False)
    return Check("apprunner access", WARN, f"cannot list AppRunner services: {stderr[:80]}", required=False)


def check_circleci_auth() -> Check:
    """Check if CircleCI CLI is authenticated."""
    code, stdout, stderr = _run(["circleci", "diagnostic"])
    if code == 0 and "OK" in (stdout + stderr):
        return Check("circleci auth", PASS, "authenticated", required=False)
    return Check("circleci auth", WARN, "not configured - deploy monitor won't work for CircleCI", required=False)


def check_s3_artifact_bucket(config: Config) -> Check:
    """Check if S3 artifact bucket is configured and accessible."""
    if not config.s3_artifact_bucket:
        return Check("s3 artifacts", WARN, "S3_ARTIFACT_BUCKET not set - artifact archival disabled", required=False)
    cmd = [
        "aws",
        "s3api",
        "head-bucket",
        "--bucket",
        config.s3_artifact_bucket,
        "--region",
        config.s3_artifact_region,
        *_aws_profile_args(config.aws_profile),
    ]
    code, _stdout, stderr = _run(cmd)
    if code == 0:
        return Check("s3 artifacts", PASS, f"bucket={config.s3_artifact_bucket} region={config.s3_artifact_region}", required=False)
    return Check("s3 artifacts", WARN, f"cannot access bucket '{config.s3_artifact_bucket}': {stderr[:80]}", required=False)


def check_k8s(config: Config) -> Check:
    """Check K8s API connectivity and agent Job creation permissions."""
    try:
        from kubernetes_asyncio import client as k8s_client
        from kubernetes_asyncio import config as k8s_config
    except ImportError:
        return Check("k8s dispatch", FAIL, "kubernetes-asyncio not installed -- run: uv add kubernetes-asyncio")

    if not config.k8s_agent_image:
        return Check("k8s dispatch", FAIL, "K8S_AGENT_IMAGE not set -- required for K8s dispatch")

    async def _probe():
        try:
            k8s_config.load_incluster_config()
        except k8s_config.ConfigException:
            try:
                await k8s_config.load_kube_config()
            except k8s_config.ConfigException:
                return Check("k8s dispatch", FAIL, "no K8s config found (not in-cluster and no kubeconfig)")

        batch_v1 = k8s_client.BatchV1Api()
        try:
            job = k8s_client.V1Job(
                metadata=k8s_client.V1ObjectMeta(name="preflight-test"),
                spec=k8s_client.V1JobSpec(
                    template=k8s_client.V1PodTemplateSpec(
                        spec=k8s_client.V1PodSpec(
                            containers=[k8s_client.V1Container(name="test", image="busybox")],
                            restart_policy="Never",
                        )
                    )
                ),
            )
            await batch_v1.create_namespaced_job(
                namespace=config.k8s_namespace,
                body=job,
                dry_run="All",
            )
            await batch_v1.api_client.close()
            return Check("k8s dispatch", PASS, f"K8s API reachable, RBAC ok (ns={config.k8s_namespace})")
        except Exception as e:
            await batch_v1.api_client.close()
            return Check("k8s dispatch", FAIL, f"K8s API error: {str(e)[:80]}")

    try:
        asyncio.get_running_loop()
        in_loop = True
    except RuntimeError:
        in_loop = False

    if in_loop:
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, _probe()).result(timeout=15)
    else:
        return asyncio.run(_probe())


def check_postgres(config: Config) -> Check:
    """Check if Postgres is reachable and the minions schema exists."""
    if not config.postgres_url:
        return Check("postgres", FAIL, "POSTGRES_URL not set (and no DB_HOST)", required=True)
    try:
        import psycopg

        with psycopg.connect(config.postgres_url, connect_timeout=5) as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 FROM information_schema.schemata WHERE schema_name = 'minions'")
            row = cur.fetchone()
            if row:
                return Check("postgres", PASS, "connected, minions schema found")
            return Check("postgres", WARN, "connected, but minions schema not found -- run dbmate up")
    except ImportError:
        return Check("postgres", FAIL, "psycopg not installed -- run: uv add psycopg[binary]")
    except Exception as e:
        return Check("postgres", FAIL, f"connection failed: {str(e)[:80]}")


def check_nats(config: Config) -> Check:
    """Check if NATS is reachable by attempting a connect/close cycle."""
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

        # Handle being called from within an already-running event loop
        # (e.g. watch_trello -> run_preflight) by running in a thread.
        try:
            asyncio.get_running_loop()
            in_loop = True
        except RuntimeError:
            in_loop = False

        if in_loop:
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(asyncio.run, _probe()).result(timeout=10)
        else:
            asyncio.run(_probe())

        # NATS_SERVER_IP is a bare host today, but the format permits
        # nats://user:pass@host — redact so a future value cannot leak.
        servers_str = ", ".join(_redact_url(s) for s in nats_config.servers)
        return Check("nats", PASS, f"connected to {servers_str} (stream={config.nats_stream})")
    except ImportError:
        return Check("nats", FAIL, "nats-py not installed -- run: uv add nats-py")
    except Exception as e:
        return Check("nats", FAIL, f"connection failed: {str(e)[:80]}")


def check_arbiter(config: Config) -> Check:
    """Validate arbiter prerequisites: NATS enabled."""
    if not config.nats_enabled:
        return Check("arbiter", FAIL, "NATS_ENABLED must be true")
    return Check("arbiter", PASS, f"prerequisites met (nats={config.nats_enabled})")


def _redact_url(url: str) -> str:
    """Strip userinfo from a URL so it is safe to log.

    redis://:hunter2@host:6379/0  ->  redis://host:6379/0

    Preflight output goes to stdout, which in Kubernetes means pod logs and every
    log aggregator downstream of them. Connection strings carry credentials, so
    they must never be printed verbatim.
    """
    try:
        parsed = urlparse(url)
        if not parsed.netloc or "@" not in parsed.netloc:
            return url
        host = parsed.netloc.rsplit("@", 1)[1]
        return urlunparse(parsed._replace(netloc=host))
    except Exception:
        # Never let redaction failure surface the raw value.
        return "<unparseable url>"


def check_redis(config: Config) -> Check:
    """Check if Redis is reachable when memory is enabled."""
    try:
        import redis as redis_lib

        r = redis_lib.Redis.from_url(config.redis_url, password=config.redis_password or None, socket_connect_timeout=5)
        r.ping()
        r.close()
        return Check("redis", PASS, f"connected ({_redact_url(config.redis_url)})")
    except ImportError:
        return Check("redis", FAIL, "redis not installed -- run: uv add redis[hiredis]")
    except Exception as e:
        # Exception text can echo the DSN back (redis-py includes it in some
        # connection errors), so redact the message too.
        return Check("redis", FAIL, f"connection failed: {_redact_url(str(e)[:200])[:80]}")


def check_langfuse(config: Config) -> Check:
    """Check Langfuse connectivity (optional, warn-only)."""
    if not config.langfuse_public_key:
        return Check("langfuse", WARN, "not configured — LLM tracing disabled", required=False)
    try:
        import urllib.request

        url = f"{config.langfuse_host}/api/public/health"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status == 200:
                return Check("langfuse", PASS, f"reachable ({config.langfuse_host})", required=False)
            return Check("langfuse", WARN, f"unexpected status {resp.status} from {config.langfuse_host}", required=False)
    except Exception as e:
        return Check("langfuse", WARN, f"unreachable ({config.langfuse_host}): {str(e)[:60]}", required=False)


def run_preflight(config: Config | None = None) -> list[Check]:
    """Run all preflight checks and return results."""
    config = config or Config.from_env()
    checks: list[Check] = []

    container = _in_container()

    # Core CLIs
    secrets_cmd = os.getenv("SECRETS_CMD", "")
    doppler_required = "doppler" in secrets_cmd and not container

    checks.append(check_cli("git", ["git", "--version"]))
    checks.append(check_cli("rg", ["rg", "--version"], required=False))
    checks.append(check_cli("doppler", ["doppler", "--version"], required=doppler_required))

    # Deploy monitor CLIs (optional)
    checks.append(check_cli("circleci", ["circleci", "version"], required=False))
    checks.append(check_cli("aws", ["aws", "--version"], required=False))

    # LiteLLM
    checks.append(check_litellm())

    # Git provider
    checks.append(check_git_provider(config))

    # Auth checks
    if shutil.which("doppler") and doppler_required:
        checks.append(check_doppler_auth())
    elif shutil.which("doppler") and not doppler_required:
        checks.append(Check("doppler auth", WARN, "doppler found but SECRETS_CMD does not use it", required=False))
    if shutil.which("circleci"):
        checks.append(check_circleci_auth())
    if shutil.which("aws"):
        checks.append(check_aws_auth(config.aws_profile))
        checks.append(check_apprunner_access(config.aws_profile))
        checks.append(check_s3_artifact_bucket(config))

    # Database backend check
    checks.append(check_postgres(config))

    # NATS check (required when enabled)
    if config.nats_enabled:
        checks.append(check_nats(config))

    # Arbiter check (when enabled)
    if config.arbiter_enabled:
        checks.append(check_arbiter(config))

    # K8s dispatch check (when enabled)
    if config.k8s_dispatch:
        checks.append(check_k8s(config))

    # Memory system (Redis, when enabled)
    if config.memory_enabled:
        checks.append(check_redis(config))

    # Langfuse (optional)
    checks.append(check_langfuse(config))

    return checks


def print_preflight(checks: list[Check]) -> bool:
    """Print preflight results and return True if all required checks pass."""
    print("\n=== Minions Suite Preflight Checks ===\n")

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
    print("\n  All checks passed. Ready to launch.\n")
    return True


def main():
    """Run preflight checks as standalone script."""
    config = Config.from_env()
    checks = run_preflight(config)
    ok = print_preflight(checks)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
