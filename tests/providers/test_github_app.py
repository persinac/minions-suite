"""GitHub App installation-token provider.

Runs entirely against a generated throwaway RSA key and a stubbed GitHub — no
real App credentials involved.
"""

import os
import time
from dataclasses import dataclass
from typing import ClassVar

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from minions.providers.github_app import (
    REFRESH_MARGIN_SECONDS,
    GitHubAppError,
    GitHubAppTokenProvider,
    build_token_provider,
)


@pytest.fixture(scope="module")
def keypair() -> tuple[str, object]:
    """A throwaway RSA keypair: (private PEM, public key object)."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    return pem, key.public_key()


@dataclass
class _FakeResponse:
    status_code: int
    _body: dict | None = None
    text: str = ""

    def json(self) -> dict:
        return self._body or {}


class _FakeClient:
    """Stands in for httpx.AsyncClient, recording the requests it receives."""

    # Deliberately class-level: shared recording state across the stubbed client
    # instances the code under test constructs internally.
    calls: ClassVar[list[dict]] = []
    responses: ClassVar[list[_FakeResponse]] = []

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, **kw):
        _FakeClient.calls.append({"url": url, "headers": headers or {}})
        if _FakeClient.responses:
            return _FakeClient.responses.pop(0)
        return _FakeResponse(201, {"token": "ghs_default", "expires_at": None})


@pytest.fixture(autouse=True)
def _stub_httpx(monkeypatch):
    _FakeClient.calls = []
    _FakeClient.responses = []
    monkeypatch.setattr("minions.providers.github_app.httpx.AsyncClient", _FakeClient)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    yield
    _FakeClient.calls = []
    _FakeClient.responses = []


def _provider(pem: str) -> GitHubAppTokenProvider:
    return GitHubAppTokenProvider(app_id="4393069", private_key=pem, installation_id="12345")


def _iso(offset_seconds: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + offset_seconds))


class TestBuildTokenProvider:
    """Absent credentials must degrade to the static-PAT path, not raise."""

    @dataclass
    class _Cfg:
        github_app_id: str = ""
        github_app_private_key: str = ""
        github_app_installation_id: str = ""

    def test_returns_none_when_unconfigured(self):
        assert build_token_provider(self._Cfg()) is None

    def test_returns_none_when_only_partly_configured(self, keypair):
        pem, _ = keypair
        cfg = self._Cfg(github_app_id="4393069", github_app_private_key=pem)  # no installation id
        assert build_token_provider(cfg) is None

    def test_returns_provider_when_fully_configured(self, keypair):
        pem, _ = keypair
        cfg = self._Cfg("4393069", pem, "12345")
        assert isinstance(build_token_provider(cfg), GitHubAppTokenProvider)


class TestJwt:
    async def test_jwt_is_signed_and_well_formed(self, keypair):
        pem, public = keypair
        await _provider(pem).token()

        auth = _FakeClient.calls[0]["headers"]["Authorization"]
        assert auth.startswith("Bearer ")
        claims = jwt.decode(auth.removeprefix("Bearer "), public, algorithms=["RS256"])

        assert claims["iss"] == "4393069"
        # iat is backdated for clock skew, exp is inside GitHub's 10-minute ceiling.
        now = time.time()
        assert claims["iat"] < now
        assert 0 < claims["exp"] - now <= 600

    async def test_escaped_newlines_in_pem_are_normalised(self, keypair):
        """Doppler and k8s secrets commonly deliver PEMs with literal \\n."""
        pem, public = keypair
        mangled = pem.replace("\n", "\\n")

        await _provider(mangled).token()

        auth = _FakeClient.calls[0]["headers"]["Authorization"]
        jwt.decode(auth.removeprefix("Bearer "), public, algorithms=["RS256"])  # would raise if broken

    async def test_unusable_key_raises_without_leaking_material(self):
        p = GitHubAppTokenProvider("4393069", "-----BEGIN PRIVATE KEY-----\nnope\n-----END PRIVATE KEY-----", "12345")
        with pytest.raises(GitHubAppError) as exc:
            await p.token()
        assert "GITHUB_APP_PRIVATE_KEY" in str(exc.value)
        assert "nope" not in str(exc.value)


class TestCaching:
    async def test_second_call_reuses_the_cached_token(self, keypair):
        pem, _ = keypair
        _FakeClient.responses = [_FakeResponse(201, {"token": "ghs_first", "expires_at": _iso(3600)})]
        p = _provider(pem)

        assert await p.token() == "ghs_first"
        assert await p.token() == "ghs_first"
        assert len(_FakeClient.calls) == 1, "should not have re-minted"

    async def test_token_inside_the_refresh_margin_is_reminted(self, keypair):
        """A token expiring in 60s must not be handed to an agent that will run for ten minutes."""
        pem, _ = keypair
        _FakeClient.responses = [
            _FakeResponse(201, {"token": "ghs_stale", "expires_at": _iso(REFRESH_MARGIN_SECONDS - 60)}),
            _FakeResponse(201, {"token": "ghs_fresh", "expires_at": _iso(3600)}),
        ]
        p = _provider(pem)

        assert await p.token() == "ghs_stale"
        assert await p.token() == "ghs_fresh"
        assert len(_FakeClient.calls) == 2

    async def test_unparseable_expiry_falls_back_to_one_hour(self, keypair):
        pem, _ = keypair
        _FakeClient.responses = [_FakeResponse(201, {"token": "ghs_x", "expires_at": "not-a-date"})]
        p = _provider(pem)
        await p.token()
        assert p._cached.expires_at > time.time() + 3000


class TestAmbientEnvironment:
    async def test_gh_token_is_exported_for_bare_gh_callers(self, keypair):
        """mcp_executor shells out to `gh` with no env override — it reads os.environ."""
        pem, _ = keypair
        _FakeClient.responses = [_FakeResponse(201, {"token": "ghs_ambient", "expires_at": _iso(3600)})]

        assert os.environ.get("GH_TOKEN") is None
        await _provider(pem).token()
        assert os.environ["GH_TOKEN"] == "ghs_ambient"


class TestErrors:
    @pytest.mark.parametrize(
        "status,expect",
        [
            (401, "GITHUB_APP_ID"),
            (404, "GITHUB_APP_INSTALLATION_ID"),
            (500, "HTTP 500"),
        ],
    )
    async def test_http_errors_are_actionable(self, keypair, status, expect):
        pem, _ = keypair
        _FakeClient.responses = [_FakeResponse(status, {}, text="upstream detail")]
        with pytest.raises(GitHubAppError) as exc:
            await _provider(pem).token()
        assert expect in str(exc.value)

    async def test_missing_token_in_body_is_an_error(self, keypair):
        pem, _ = keypair
        _FakeClient.responses = [_FakeResponse(201, {"expires_at": _iso(3600)})]
        with pytest.raises(GitHubAppError):
            await _provider(pem).token()

    def test_constructor_rejects_partial_credentials(self, keypair):
        pem, _ = keypair
        with pytest.raises(ValueError):
            GitHubAppTokenProvider("", pem, "12345")
