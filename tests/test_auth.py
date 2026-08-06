"""Auth layer tests.

Security-critical, so failure paths get at least as much attention as the happy
path. Each test names the attack or mistake it prevents.
"""

from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from tests.conftest import (
    ADMIN_EMAIL,
    SERVICE_KEY,
    TEST_KID,
    USER_ID,
    attacker_token,
    auth,
    make_token,
)

# ── Happy path ─────────────────────────────────────────────────────────────


def test_health_needs_no_auth(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_valid_es256_token_is_accepted(client: TestClient) -> None:
    r = client.post("/auth/session", headers=auth(make_token()))
    assert r.status_code == 200
    body = r.json()
    assert body["user_id"] == USER_ID
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
    assert client.post("/auth/session", headers=auth("not-a-jwt")).status_code == 401


# ── Signature and algorithm attacks ────────────────────────────────────────


def test_token_signed_with_untrusted_key_is_rejected(client: TestClient) -> None:
    """The core check: a well-formed ES256 token we did not sign is worthless."""
    assert client.post("/auth/session", headers=auth(attacker_token())).status_code == 401


def test_alg_none_is_rejected(client: TestClient) -> None:
    """The classic JWT bypass: strip the signature and claim alg=none."""
    token = jwt.encode({"sub": "abc", "exp": int(time.time()) + 3600}, key="", algorithm="none")
    assert client.post("/auth/session", headers=auth(token)).status_code == 401


def test_hs256_is_rejected_even_though_it_is_a_real_supabase_algorithm(
    client: TestClient,
) -> None:
    """HS256 is deliberately unsupported.

    An HMAC secret both verifies AND mints, so accepting HS256 would mean this
    service could forge a session for any user. Symmetric tokens must never
    authenticate here, regardless of the secret presented.
    """
    token = jwt.encode(
        {"sub": USER_ID, "exp": int(time.time()) + 3600, "role": "authenticated"},
        "any-shared-secret-at-all",
        algorithm="HS256",
    )
    assert client.post("/auth/session", headers=auth(token)).status_code == 401


def test_algorithm_confusion_downgrade_is_rejected(client: TestClient) -> None:
    """Presenting the public key as an HMAC secret must not verify."""
    from tests.conftest import JWKS

    token = jwt.encode(
        {"sub": USER_ID, "exp": int(time.time()) + 3600},
        json_dumps_key(JWKS),
        algorithm="HS256",
    )
    assert client.post("/auth/session", headers=auth(token)).status_code == 401


def json_dumps_key(jwks: dict[str, object]) -> str:
    import json

    return json.dumps(jwks)


def test_unlisted_algorithm_is_rejected(client: TestClient) -> None:
    """alg must come from our allow-list, not from the token."""
    key = ec.generate_private_key(ec.SECP521R1())
    token = jwt.encode({"sub": USER_ID, "exp": int(time.time()) + 3600}, key, algorithm="ES512")
    assert client.post("/auth/session", headers=auth(token)).status_code == 401


def test_unknown_kid_is_rejected(client: TestClient) -> None:
    """A token naming a signing key that isn't in our JWKS must not verify."""
    r = client.post("/auth/session", headers=auth(make_token(kid="some-other-key")))
    assert r.status_code == 401


def test_tampered_payload_is_rejected(client: TestClient) -> None:
    """Swapping in a different payload invalidates the signature."""
    good = make_token()
    forged = attacker_token(sub="99999999-9999-9999-9999-999999999999")
    head, _, sig = good.split(".")
    _, forged_payload, _ = forged.split(".")
    r = client.post("/auth/session", headers=auth(f"{head}.{forged_payload}.{sig}"))
    assert r.status_code == 401


# ── Claim validation ───────────────────────────────────────────────────────


def test_expired_token_is_rejected(client: TestClient) -> None:
    assert client.post("/auth/session", headers=auth(make_token(exp_delta=-60))).status_code == 401


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
    assert client.post("/auth/session", headers=auth(make_token(omit=("sub",)))).status_code == 401


def test_missing_exp_is_rejected(client: TestClient) -> None:
    """A token without an expiry would be valid forever."""
    assert client.post("/auth/session", headers=auth(make_token(omit=("exp",)))).status_code == 401


def test_anon_role_is_rejected(client: TestClient) -> None:
    """The Supabase anon key is not a user. Accepting it opens the front door."""
    assert client.post("/auth/session", headers=auth(make_token(role="anon"))).status_code == 401


# ── Clock skew (a REAL bug, found against the live project) ────────────────


def test_token_issued_slightly_in_the_future_is_accepted(client: TestClient) -> None:
    """Regression: the first live end-to-end test failed with
    `ImmatureSignatureError: The token is not yet valid (iat)`.

    Supabase's clock was a moment ahead of ours, so a freshly-minted, perfectly
    valid token was rejected. Without leeway this rejects every sign-in whenever
    our server's clock drifts behind theirs — an availability bug that only a
    real token could reveal.
    """
    r = client.post("/auth/session", headers=auth(make_token(iat_delta=20)))
    assert r.status_code == 200


def test_token_expiring_within_the_skew_window_is_accepted(client: TestClient) -> None:
    """The same leeway must apply to `exp` — a slow issuer, not a fast one."""
    r = client.post("/auth/session", headers=auth(make_token(exp_delta=-20)))
    assert r.status_code == 200


def test_leeway_is_bounded_and_does_not_excuse_a_real_expiry(client: TestClient) -> None:
    """Tolerating skew must not become tolerating expired tokens."""
    r = client.post("/auth/session", headers=auth(make_token(exp_delta=-3600)))
    assert r.status_code == 401


def test_leeway_does_not_accept_a_token_from_far_in_the_future(client: TestClient) -> None:
    """A token issued an hour from now is not skew — it is wrong."""
    r = client.post("/auth/session", headers=auth(make_token(iat_delta=3600)))
    assert r.status_code == 401


def test_skew_allowance_is_modest() -> None:
    """A large allowance would meaningfully extend every token's life."""
    from carigma_api.auth.jwt_verifier import CLOCK_SKEW_LEEWAY_SECONDS

    assert 0 < CLOCK_SKEW_LEEWAY_SECONDS <= 120


# ── Failures leak nothing ──────────────────────────────────────────────────


def test_all_failures_return_identical_body(client: TestClient) -> None:
    """Distinguishable errors tell an attacker which knob to turn next."""
    tokens = [
        make_token(exp_delta=-60),
        attacker_token(),
        make_token(audience="wrong"),
        make_token(kid="unknown"),
        "not-a-jwt",
    ]
    bodies = set()
    for token in tokens:
        r = client.post("/auth/session", headers=auth(token))
        assert r.status_code == 401
        bodies.add(r.text)
    assert len(bodies) == 1, "401 bodies differ between failure modes"


# ── Admin gate ─────────────────────────────────────────────────────────────


def test_admin_allowed(client: TestClient) -> None:
    r = client.get("/auth/admin-probe", headers=auth(make_token(email=ADMIN_EMAIL)))
    assert r.status_code == 200


def test_admin_flag_reported_for_admin(client: TestClient) -> None:
    r = client.post("/auth/session", headers=auth(make_token(email=ADMIN_EMAIL)))
    assert r.json()["is_admin"] is True


def test_non_admin_gets_403_not_401(client: TestClient) -> None:
    """Authenticated but unauthorized is 403 — a distinct, correct status."""
    assert client.get("/auth/admin-probe", headers=auth(make_token())).status_code == 403


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
    assert client.get("/auth/service-probe", headers={"X-Service-Key": "wrong"}).status_code == 401


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


def test_no_jwks_configured_rejects_everything() -> None:
    """Without a JWKS URL there is nothing to verify against — reject, never allow."""
    from carigma_api.auth.jwt_verifier import InvalidTokenError, JWTVerifier
    from carigma_api.config import Settings

    verifier = JWTVerifier(Settings(supabase_url="", supabase_jwks_url=""))
    with pytest.raises(InvalidTokenError):
        verifier.verify(make_token())


def test_jwks_url_derived_from_supabase_url() -> None:
    from carigma_api.config import Settings

    s = Settings(supabase_url="https://abc.supabase.co/")
    assert s.jwks_url == "https://abc.supabase.co/auth/v1/.well-known/jwks.json"


def test_config_has_no_hs256_secret_field() -> None:
    """Regression guard: re-adding a shared secret would give this service the
    ability to MINT tokens, not just verify them."""
    from carigma_api.config import Settings

    assert not hasattr(Settings(), "supabase_jwt_secret")


def test_allow_list_excludes_symmetric_algorithms() -> None:
    from carigma_api.auth.jwt_verifier import ALLOWED_ALGORITHMS

    assert "ES256" in ALLOWED_ALGORITHMS
    assert not any(a.startswith("HS") for a in ALLOWED_ALGORITHMS)


def test_rotated_key_is_picked_up_without_redeploy(monkeypatch: pytest.MonkeyPatch) -> None:
    """A key rotated inside the cache TTL must not cause a wave of 401s: the
    verifier refetches JWKS once on a miss."""
    import json

    from jwt import PyJWKClient
    from jwt.algorithms import ECAlgorithm

    from carigma_api.auth.jwt_verifier import JWTVerifier
    from carigma_api.config import Settings
    from tests.conftest import TEST_AUDIENCE, TEST_SUPABASE_URL

    old_key = ec.generate_private_key(ec.SECP256R1())
    new_key = ec.generate_private_key(ec.SECP256R1())

    def jwk_for(k: ec.EllipticCurvePrivateKey, kid: str) -> dict[str, object]:
        d = json.loads(ECAlgorithm.to_jwk(k.public_key()))
        d.update({"kid": kid, "alg": "ES256", "use": "sig"})
        return d

    state = {"keys": [jwk_for(old_key, "old")]}
    monkeypatch.setattr(PyJWKClient, "fetch_data", lambda self: state)

    verifier = JWTVerifier(
        Settings(supabase_url=TEST_SUPABASE_URL, supabase_jwt_audience=TEST_AUDIENCE)
    )
    # Warm the cache with the old key.
    verifier.verify(make_token(key=old_key, kid="old"))

    # Rotate: the cache still holds only "old".
    state["keys"] = [jwk_for(new_key, "new")]
    user = verifier.verify(make_token(key=new_key, kid="new"))
    assert user.id == USER_ID


def test_kid_is_required_to_match_a_published_key() -> None:
    """Sanity guard on the fixture itself."""
    from tests.conftest import JWKS

    assert JWKS["keys"][0]["kid"] == TEST_KID
