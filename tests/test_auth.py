"""Auth layer tests.

The brief calls this security-critical, so the failure paths get at least as
much attention as the happy path. Each test names the attack or mistake it
prevents.
"""

from __future__ import annotations

import time

import jwt
import pytest
from fastapi.testclient import TestClient
from tests.conftest import (
    ADMIN_EMAIL,
    SERVICE_KEY,
    TEST_AUDIENCE,
    TEST_ISSUER,
    TEST_SECRET,
    auth,
    make_token,
)

# ── Happy path ─────────────────────────────────────────────────────────────


def test_health_needs_no_auth(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_valid_token_is_accepted(client: TestClient) -> None:
    r = client.post("/auth/session", headers=auth(make_token()))
    assert r.status_code == 200
    body = r.json()
    assert body["user_id"] == "11111111-1111-1111-1111-111111111111"
    assert body["email"] == "user@example.com"
    assert body["is_admin"] is False


def test_email_is_normalised_to_lowercase(client: TestClient) -> None:
    """Admin matching is by email; case must not create a bypass or a miss."""
    r = client.post("/auth/session", headers=auth(make_token(email="MiXeD@Example.COM")))
    assert r.status_code == 200
    assert r.json()["email"] == "mixed@example.com"


# ── Missing / malformed credentials ────────────────────────────────────────


def test_no_token_is_rejected(client: TestClient) -> None:
    assert client.post("/auth/session").status_code == 401


def test_empty_bearer_is_rejected(client: TestClient) -> None:
    r = client.post("/auth/session", headers={"Authorization": "Bearer "})
    assert r.status_code == 401


def test_wrong_scheme_is_rejected(client: TestClient) -> None:
    """A Basic-auth header must not be accepted as a bearer token."""
    r = client.post("/auth/session", headers={"Authorization": "Basic abc123"})
    assert r.status_code == 401


def test_garbage_token_is_rejected(client: TestClient) -> None:
    r = client.post("/auth/session", headers=auth("not-a-jwt"))
    assert r.status_code == 401


# ── Signature and algorithm attacks ────────────────────────────────────────


def test_token_signed_with_wrong_secret_is_rejected(client: TestClient) -> None:
    """The core check: a well-formed token we did not sign is worthless."""
    r = client.post("/auth/session", headers=auth(make_token(secret="attacker-secret")))
    assert r.status_code == 401


def test_alg_none_is_rejected(client: TestClient) -> None:
    """The classic JWT bypass: strip the signature and claim alg=none."""
    token = jwt.encode({"sub": "abc", "exp": int(time.time()) + 3600}, key="", algorithm="none")
    assert client.post("/auth/session", headers=auth(token)).status_code == 401


def test_unlisted_algorithm_is_rejected(client: TestClient) -> None:
    """alg must come from the allow-list, not from the token."""
    token = make_token(algorithm="HS512")
    assert client.post("/auth/session", headers=auth(token)).status_code == 401


def test_tampered_payload_is_rejected(client: TestClient) -> None:
    """Editing a claim invalidates the signature."""
    token = make_token()
    head, payload, sig = token.split(".")
    forged = make_token(sub="99999999-9999-9999-9999-999999999999", secret="other")
    _, forged_payload, _ = forged.split(".")
    assert (
        client.post("/auth/session", headers=auth(f"{head}.{forged_payload}.{sig}")).status_code
        == 401
    )


# ── Claim validation ───────────────────────────────────────────────────────


def test_expired_token_is_rejected(client: TestClient) -> None:
    r = client.post("/auth/session", headers=auth(make_token(exp_delta=-60)))
    assert r.status_code == 401


def test_wrong_audience_is_rejected(client: TestClient) -> None:
    r = client.post("/auth/session", headers=auth(make_token(audience="some-other-app")))
    assert r.status_code == 401


def test_wrong_issuer_is_rejected(client: TestClient) -> None:
    """A token from a different Supabase project must not work here."""
    r = client.post(
        "/auth/session", headers=auth(make_token(issuer="https://evil.supabase.co/auth/v1"))
    )
    assert r.status_code == 401


def test_missing_sub_is_rejected(client: TestClient) -> None:
    r = client.post("/auth/session", headers=auth(make_token(omit=("sub",))))
    assert r.status_code == 401


def test_missing_exp_is_rejected(client: TestClient) -> None:
    """A token without an expiry would be valid forever."""
    r = client.post("/auth/session", headers=auth(make_token(omit=("exp",))))
    assert r.status_code == 401


def test_anon_role_is_rejected(client: TestClient) -> None:
    """The Supabase anon key is not a user. Accepting it opens the front door."""
    r = client.post("/auth/session", headers=auth(make_token(role="anon")))
    assert r.status_code == 401


# ── Failures leak nothing ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "token",
    [
        make_token(exp_delta=-60),
        make_token(secret="attacker-secret"),
        make_token(audience="wrong"),
        "not-a-jwt",
    ],
    ids=["expired", "bad-signature", "wrong-audience", "malformed"],
)
def test_all_failures_return_identical_body(client: TestClient, token: str) -> None:
    """Distinguishable errors tell an attacker which knob to turn next."""
    r = client.post("/auth/session", headers=auth(token))
    assert r.status_code == 401
    assert r.json()["detail"] == "Not authenticated."


# ── Admin gate ─────────────────────────────────────────────────────────────


def test_admin_allowed(client: TestClient) -> None:
    r = client.get("/auth/admin-probe", headers=auth(make_token(email=ADMIN_EMAIL)))
    assert r.status_code == 200


def test_admin_flag_reported_for_admin(client: TestClient) -> None:
    r = client.post("/auth/session", headers=auth(make_token(email=ADMIN_EMAIL)))
    assert r.json()["is_admin"] is True


def test_non_admin_gets_403_not_401(client: TestClient) -> None:
    """Authenticated but unauthorized is 403 — a distinct, correct status."""
    r = client.get("/auth/admin-probe", headers=auth(make_token()))
    assert r.status_code == 403


def test_admin_route_still_requires_a_token(client: TestClient) -> None:
    assert client.get("/auth/admin-probe").status_code == 401


def test_client_cannot_self_declare_admin(client: TestClient) -> None:
    """A crafted claim must not grant admin — the allow-list is the only source."""
    token = make_token(email="user@example.com", role="admin")
    assert client.get("/auth/admin-probe", headers=auth(token)).status_code == 403


def test_empty_allow_list_means_nobody_is_admin(client: TestClient, settings) -> None:  # type: ignore[no-untyped-def]
    """Safe default: an unset ADMIN_EMAILS must not mean 'everyone'."""
    from carigma_api.config import get_settings

    settings.admin_emails = ""
    client.app.dependency_overrides[get_settings] = lambda: settings
    r = client.get("/auth/admin-probe", headers=auth(make_token(email=ADMIN_EMAIL)))
    assert r.status_code == 403


# ── Service role ───────────────────────────────────────────────────────────


def test_service_role_accepts_correct_key(client: TestClient) -> None:
    r = client.get("/auth/service-probe", headers={"X-Service-Key": SERVICE_KEY})
    assert r.status_code == 200


def test_service_role_rejects_wrong_key(client: TestClient) -> None:
    r = client.get("/auth/service-probe", headers={"X-Service-Key": "wrong"})
    assert r.status_code == 401


def test_service_role_rejects_missing_key(client: TestClient) -> None:
    assert client.get("/auth/service-probe").status_code == 401


def test_user_jwt_does_not_open_the_service_route(client: TestClient) -> None:
    """A signed-in user must not reach server-to-server endpoints."""
    r = client.get("/auth/service-probe", headers=auth(make_token(email=ADMIN_EMAIL)))
    assert r.status_code == 401


# ── Ownership ──────────────────────────────────────────────────────────────


def test_assert_owns_allows_owner() -> None:
    from carigma_api.auth.dependencies import assert_owns
    from carigma_api.auth.jwt_verifier import AuthenticatedUser

    user = AuthenticatedUser(id="user-1", email="a@b.com", role="authenticated", claims={})
    assert_owns(user, "user-1")  # must not raise


def test_assert_owns_blocks_other_users_resource() -> None:
    """Authentication is not authorization — guessing an id must not work."""
    from fastapi import HTTPException

    from carigma_api.auth.dependencies import assert_owns
    from carigma_api.auth.jwt_verifier import AuthenticatedUser

    user = AuthenticatedUser(id="user-1", email="a@b.com", role="authenticated", claims={})
    with pytest.raises(HTTPException) as exc:
        assert_owns(user, "user-2")
    assert exc.value.status_code == 403


# ── Verifier configuration ─────────────────────────────────────────────────


def test_hs256_rejected_when_no_secret_configured() -> None:
    """Without a secret there is nothing to verify against — reject, never allow."""
    from carigma_api.auth.jwt_verifier import InvalidTokenError, JWTVerifier
    from carigma_api.config import Settings

    verifier = JWTVerifier(Settings(supabase_url="", supabase_jwt_secret=""))
    with pytest.raises(InvalidTokenError):
        verifier.verify(make_token(issuer=None, audience=None))


def test_jwks_url_derived_from_supabase_url() -> None:
    from carigma_api.config import Settings

    s = Settings(supabase_url="https://abc.supabase.co/")
    assert s.jwks_url == "https://abc.supabase.co/auth/v1/.well-known/jwks.json"


def test_audience_and_issuer_round_trip() -> None:
    """Guards the fixture itself: these claims must match what we verify."""
    decoded = jwt.decode(
        make_token(),
        TEST_SECRET,
        algorithms=["HS256"],
        audience=TEST_AUDIENCE,
        issuer=TEST_ISSUER,
    )
    assert decoded["sub"]
