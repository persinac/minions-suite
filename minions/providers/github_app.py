"""GitHub App installation tokens.

A GitHub App mints short-lived (1 hour) installation tokens instead of holding a
long-lived personal access token. Nothing has to be rotated by hand, the grant is
scoped to the repositories the App is installed on, and access is revoked by
uninstalling rather than by hunting down a PAT.

Flow, per GitHub's documented exchange:

    1. Build a JWT signed RS256 with the App's private key (iss = App ID)
    2. POST /app/installations/{installation_id}/access_tokens with that JWT
    3. Receive {"token": "ghs_...", "expires_at": "<iso8601>"}, valid ~1 hour

The token is cached and reminted shortly before it expires.

Why this also writes os.environ["GH_TOKEN"]
------------------------------------------
Two call paths shell out to the `gh` CLI and they do not agree on how the token
gets there:

  - providers/git.py:_run_gh builds an explicit env with GH_TOKEN
  - agents/tools/mcp_executor.py invokes `gh pr create` / `gh pr checks` with NO
    env override, inheriting the ambient process environment

The second is the path agents actually use to open PRs. Threading a token object
through only the first would leave agent PR creation on a stale or missing token,
and would need re-plumbing every time a new `gh` call site is added. Refreshing
the process environment covers both, and every future call site, from one place.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime

import httpx

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"

# Remint this long before the token actually expires. GitHub issues 1-hour
# tokens; a job that starts at T+59m must not hand a 60-second token to an agent
# that will still be pushing commits ten minutes later.
REFRESH_MARGIN_SECONDS = 300

# GitHub rejects a JWT with exp more than 10 minutes out. Stay under it, and
# backdate iat to absorb clock skew between here and GitHub (their docs
# explicitly recommend this).
JWT_LIFETIME_SECONDS = 540
JWT_CLOCK_SKEW_SECONDS = 60


@dataclass
class _CachedToken:
    value: str
    expires_at: float  # epoch seconds

    def is_fresh(self) -> bool:
        return time.time() < (self.expires_at - REFRESH_MARGIN_SECONDS)


class GitHubAppError(RuntimeError):
    """Raised when an installation token cannot be obtained."""


class GitHubAppTokenProvider:
    """Mints and caches GitHub App installation tokens."""

    def __init__(self, app_id: str, private_key: str, installation_id: str):
        if not app_id or not private_key or not installation_id:
            raise ValueError("app_id, private_key and installation_id are all required")

        self.app_id = str(app_id).strip()
        self.installation_id = str(installation_id).strip()
        # Doppler and Kubernetes secrets both tend to deliver PEMs with escaped
        # newlines. cryptography rejects those outright, with an error that does
        # not mention newlines at all.
        self.private_key = private_key.replace("\\n", "\n").strip()

        self._cached: _CachedToken | None = None
        self._lock = asyncio.Lock()

    # -- JWT ------------------------------------------------------------------

    def _build_jwt(self) -> str:
        import jwt  # pyjwt[crypto]

        now = int(time.time())
        payload = {
            "iat": now - JWT_CLOCK_SKEW_SECONDS,
            "exp": now + JWT_LIFETIME_SECONDS,
            "iss": self.app_id,
        }
        try:
            return jwt.encode(payload, self.private_key, algorithm="RS256")
        except Exception as e:
            # Never let the exception text carry key material.
            raise GitHubAppError(f"could not sign App JWT ({type(e).__name__}) — check GITHUB_APP_PRIVATE_KEY is a full PEM") from None

    # -- Token ----------------------------------------------------------------

    async def token(self) -> str:
        """Return a valid installation token, reminting if needed."""
        async with self._lock:
            if self._cached and self._cached.is_fresh():
                return self._cached.value
            return await self._mint()

    async def _mint(self) -> str:
        url = f"{GITHUB_API}/app/installations/{self.installation_id}/access_tokens"
        headers = {
            "Authorization": f"Bearer {self._build_jwt()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(url, headers=headers)
        except httpx.HTTPError as e:
            raise GitHubAppError(f"could not reach GitHub to mint a token: {e}") from None

        if resp.status_code == 401:
            raise GitHubAppError("GitHub rejected the App JWT (401) — check GITHUB_APP_ID matches the private key")
        if resp.status_code == 404:
            raise GitHubAppError(
                f"installation {self.installation_id} not found (404) — check GITHUB_APP_INSTALLATION_ID, and that the App is still installed"
            )
        if resp.status_code != 201:
            raise GitHubAppError(f"token request failed: HTTP {resp.status_code} {resp.text[:160]}")

        body = resp.json()
        token = body.get("token")
        if not token:
            raise GitHubAppError("GitHub returned no token in the response body")

        expires_at = self._parse_expiry(body.get("expires_at"))
        self._cached = _CachedToken(value=token, expires_at=expires_at)

        # See the module docstring: this is what makes ambient-env `gh` callers work.
        os.environ["GH_TOKEN"] = token

        logger.info(
            "Minted GitHub App installation token for installation %s (expires in %ds)",
            self.installation_id,
            int(expires_at - time.time()),
        )
        return token

    @staticmethod
    def _parse_expiry(raw: str | None) -> float:
        """Parse GitHub's expires_at, falling back to a conservative 1 hour."""
        if not raw:
            return time.time() + 3600
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except ValueError:
            logger.warning("Could not parse expires_at %r — assuming 1 hour", raw)
            return time.time() + 3600


def build_token_provider(config) -> GitHubAppTokenProvider | None:
    """Return a provider when App credentials are configured, else None.

    None means "fall back to the static GH_TOKEN", which keeps local development
    and any existing PAT-based deployment working unchanged.
    """
    if not (config.github_app_id and config.github_app_private_key and config.github_app_installation_id):
        return None
    return GitHubAppTokenProvider(
        app_id=config.github_app_id,
        private_key=config.github_app_private_key,
        installation_id=config.github_app_installation_id,
    )


# Process-wide singleton. The engine refreshes it once per poll cycle; every
# `gh` invocation downstream then reads a current token out of the environment.
_provider: GitHubAppTokenProvider | None = None
_provider_built = False


async def ensure_token(config) -> str | None:
    """Refresh os.environ["GH_TOKEN"] if this deployment uses a GitHub App.

    Returns the token, or None when App auth is not configured (in which case
    GH_TOKEN is whatever the environment already supplied — a PAT, typically).

    Cheap to call repeatedly: the underlying provider only hits GitHub when the
    cached token is inside its refresh margin.

    Never raises. A failure here must not take down the poll loop — GitHub being
    briefly unreachable should degrade git operations, not stop job processing.
    """
    global _provider, _provider_built

    if not _provider_built:
        try:
            _provider = build_token_provider(config)
        except ValueError as e:
            logger.error("GitHub App credentials are malformed: %s", e)
            _provider = None
        _provider_built = True
        if _provider:
            logger.info("GitHub App auth enabled (app_id=%s, installation=%s)", _provider.app_id, _provider.installation_id)

    if _provider is None:
        return None

    try:
        return await _provider.token()
    except GitHubAppError as e:
        logger.error("Could not refresh GitHub App token: %s", e)
        return None


def reset_token_provider() -> None:
    """Drop the cached provider. For tests, and for config reloads."""
    global _provider, _provider_built
    _provider = None
    _provider_built = False
