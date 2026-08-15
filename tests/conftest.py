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
from carigma_api.services.ratelimit import limiter
from tests import _contract_recorder

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
        _contract_recorder.use_routes(app)

        # Transcribe every response shape this suite produces. The web checks
        # its TypeScript interfaces against the result, because types and
        # fixtures can agree with each other while both disagree with the
        # server — which is exactly what happened to the Naukri lens.
        inner = c.request

        def recording(method: str, url: Any, **kwargs: Any) -> Any:
            response = inner(method, url, **kwargs)
            try:
                _contract_recorder.record(
                    method, str(response.request.url.path), response.status_code, response.json()
                )
            except Exception:  # noqa: BLE001, S110 — a transcript must never
                # fail the thing it is transcribing. A non-JSON body (SSE, a
                # redirect) is normal and not worth a log line per request.
                pass
            return response

        c.request = recording  # type: ignore[method-assign]
        yield c

    app.dependency_overrides.clear()
    get_settings.cache_clear()


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def _fresh_rate_limiter() -> None:
    """Each test starts with full buckets.

    The limiter is module-global and process-lifetime by design — which is
    correct in production and wrong for a test suite, where twenty tests share
    one user id and would exhaust the bucket partway through the file. (Found
    exactly that way: the score tests passed alone and failed together, which
    was the limiter doing its job.)
    """
    limiter.reset()


@pytest.fixture(autouse=True, scope="session")
def _hermetic_settings() -> Iterator[None]:
    """Tests must never read the developer's `.env`.

    Found the moment PAYMENTS_ENABLED=true was added locally: a test asserting
    the shipped default is OFF started failing, because bare `Settings()` reads
    the dotenv file. That is a test-isolation defect, not a test bug — a suite
    whose result depends on an untracked local file tells you about that
    laptop, not about the code. It also means CI and local disagree, which is
    the worst way to find out.

    Disabling `env_file` for the session makes every bare `Settings()` fall
    back to the FIELD DEFAULTS, which is what tests about defaults should see.
    Tests needing real values build them explicitly via the `settings` fixture.
    """
    original = Settings.model_config.get("env_file")
    Settings.model_config["env_file"] = None
    yield
    Settings.model_config["env_file"] = original


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Run the contract comparison LAST.

    It compares against what the rest of the suite recorded, and pytest
    collects alphabetically — so by default it ran near the front, saw an
    almost empty transcript, and reported the entire API as deleted. A check
    whose result depends on collection order is not a check.
    """
    items.sort(key=lambda item: item.fspath.basename == "test_contract_keys.py")


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Refresh the manifest when asked, never silently.

    `UPDATE_CONTRACT_KEYS=1 pytest` rewrites it; otherwise
    `test_contract_keys.py` compares and fails with the diff. A manifest that
    regenerated itself on every run would agree with any change, including the
    rename it exists to catch.

    Guarded on a substantial transcript rather than on exit status: the drift
    test fails by design before a regeneration, so requiring a green run would
    make the manifest unregenerable. Requiring a full recording instead means a
    partial run (`pytest tests/test_naukri.py`) can never truncate it.
    """
    import os

    if os.environ.get("UPDATE_CONTRACT_KEYS") and len(_contract_recorder.RECORDED) > 20:
        _contract_recorder.write_manifest()
