"""Test fixtures.

Tokens are signed with a **real ECC P-256 key** generated per test session, and
the verifier fetches the matching public key from a stubbed JWKS endpoint. Only
the network fetch is stubbed — signature verification, `kid` lookup, `alg`
pinning, and claim validation all run for real. Mocking the verifier itself
would test nothing worth testing.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from jwt import PyJWKClient
from jwt.algorithms import ECAlgorithm

from carigma_api.config import Settings, get_settings

TEST_SUPABASE_URL = "https://test-project.supabase.co"
TEST_ISSUER = f"{TEST_SUPABASE_URL}/auth/v1"
TEST_AUDIENCE = "authenticated"
TEST_KID = "test-key-1"

ADMIN_EMAIL = "admin@carigma.in"
USER_EMAIL = "user@example.com"
SERVICE_KEY = "service-role-key-for-tests"
USER_ID = "11111111-1111-1111-1111-111111111111"

# ── Keys ───────────────────────────────────────────────────────────────────
# The project key (trusted) and an attacker key (never trusted).
_PRIVATE_KEY = ec.generate_private_key(ec.SECP256R1())
_ATTACKER_KEY = ec.generate_private_key(ec.SECP256R1())


def _public_jwk(private_key: ec.EllipticCurvePrivateKey, kid: str) -> dict[str, Any]:
    jwk: dict[str, Any] = json.loads(ECAlgorithm.to_jwk(private_key.public_key()))
    jwk.update({"kid": kid, "alg": "ES256", "use": "sig"})
    return jwk


JWKS = {"keys": [_public_jwk(_PRIVATE_KEY, TEST_KID)]}


@pytest.fixture(autouse=True)
def _stub_jwks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serve our JWKS instead of hitting the network.

    Patching `fetch_data` (not `get_signing_key_from_jwt`) keeps PyJWT's real
    kid-matching and key-parsing under test.
    """
    monkeypatch.setattr(PyJWKClient, "fetch_data", lambda self: JWKS)


def make_token(
    *,
    sub: str = USER_ID,
    email: str = USER_EMAIL,
    role: str = "authenticated",
    exp_delta: int = 3600,
    iat_delta: int = 0,
    issuer: str | None = TEST_ISSUER,
    audience: str | None = TEST_AUDIENCE,
    key: Any = None,
    algorithm: str = "ES256",
    kid: str | None = TEST_KID,
    omit: tuple[str, ...] = (),
) -> str:
    """Build a signed test token. `omit` drops claims to test required-claim
    handling; `key`/`algorithm` let a test forge or downgrade."""
    now = int(time.time())
    claims: dict[str, Any] = {
        "sub": sub,
        "email": email,
        "role": role,
        # iat_delta > 0 simulates an issuer whose clock runs ahead of ours —
        # the real Supabase clock-skew case.
        "iat": now + iat_delta,
        "exp": now + exp_delta,
    }
    if issuer is not None:
        claims["iss"] = issuer
    if audience is not None:
        claims["aud"] = audience
    for claim in omit:
        claims.pop(claim, None)

    headers = {"kid": kid} if kid else {}
    signing_key = key if key is not None else _PRIVATE_KEY
    return jwt.encode(claims, signing_key, algorithm=algorithm, headers=headers)


def attacker_token(**kwargs: Any) -> str:
    """A well-formed ES256 token signed with a key we do not trust."""
    return make_token(key=_ATTACKER_KEY, **kwargs)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        environment="test",
        supabase_url=TEST_SUPABASE_URL,
        supabase_jwt_audience=TEST_AUDIENCE,
        supabase_service_key=SERVICE_KEY,
        supabase_anon_key="test-anon-key",
        anthropic_api_key="test-anthropic-key",
        admin_emails=ADMIN_EMAIL,
        cors_origins="http://localhost:5173",
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    from carigma_api.auth.jwt_verifier import JWTVerifier
    from carigma_api.main import create_app

    get_settings.cache_clear()
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings

    with TestClient(app) as c:
        # Rebuild against the test settings (lifespan built it from real env).
        app.state.jwt_verifier = JWTVerifier(settings)
        yield c

    app.dependency_overrides.clear()
    get_settings.cache_clear()


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
