"""Test fixtures.

Tokens here are signed with a throwaway HS256 secret so the whole auth layer —
signature, exp, aud, iss, alg pinning — is exercised for real rather than
mocked out. Mocking the verifier would test nothing worth testing.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

import jwt
import pytest
from fastapi.testclient import TestClient

from carigma_api.config import Settings, get_settings

# 32+ bytes: RFC 7518 §3.2 requires an HMAC key at least as long as the hash
# output, and PyJWT warns below that.
TEST_SECRET = "test-secret-not-a-real-key-0123456789abcdef"
TEST_SUPABASE_URL = "https://test-project.supabase.co"
TEST_ISSUER = f"{TEST_SUPABASE_URL}/auth/v1"
TEST_AUDIENCE = "authenticated"

ADMIN_EMAIL = "admin@carigma.in"
USER_EMAIL = "user@example.com"
SERVICE_KEY = "service-role-key-for-tests"


def make_token(
    *,
    sub: str = "11111111-1111-1111-1111-111111111111",
    email: str = USER_EMAIL,
    role: str = "authenticated",
    exp_delta: int = 3600,
    issuer: str | None = TEST_ISSUER,
    audience: str | None = TEST_AUDIENCE,
    secret: str = TEST_SECRET,
    algorithm: str = "HS256",
    omit: tuple[str, ...] = (),
) -> str:
    """Build a signed test token. `omit` drops claims to test required-claim
    handling."""
    now = int(time.time())
    claims: dict[str, Any] = {
        "sub": sub,
        "email": email,
        "role": role,
        "iat": now,
        "exp": now + exp_delta,
    }
    if issuer is not None:
        claims["iss"] = issuer
    if audience is not None:
        claims["aud"] = audience
    for key in omit:
        claims.pop(key, None)
    return jwt.encode(claims, secret, algorithm=algorithm)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        environment="test",
        supabase_url=TEST_SUPABASE_URL,
        supabase_jwt_secret=TEST_SECRET,
        supabase_jwt_audience=TEST_AUDIENCE,
        supabase_service_key=SERVICE_KEY,
        admin_emails=ADMIN_EMAIL,
        cors_origins="http://localhost:5173",
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    from carigma_api.main import create_app

    get_settings.cache_clear()
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings

    # The verifier is built in lifespan from the real settings; rebuild it here
    # against the test settings so tokens above actually verify.
    from carigma_api.auth.jwt_verifier import JWTVerifier

    with TestClient(app) as c:
        app.state.jwt_verifier = JWTVerifier(settings)
        yield c

    app.dependency_overrides.clear()
    get_settings.cache_clear()


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
