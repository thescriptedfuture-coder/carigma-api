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


# ── Algorithm confusion ───────────────────────────────────────────────────
#
# THE ATTACK: sign an HS256 token using the server's PUBLIC key as the HMAC
# secret. A verifier that takes `alg` from the token and hands its "key" to
# whichever algorithm was named will check an HMAC with a value the attacker
# can download — and accept the forgery.
#
# ## Why the forgery is built by hand
#
# This test used `jwt.encode(..., algorithm="HS256")` with the JWKS as the
# secret. PyJWT 2.14 — a MINOR release, inside our `<3` ceiling — started
# refusing to encode a key that looks like a JWK: "should not be used directly
# as an HMAC secret". The library added the same defence the API has, and in
# doing so made the test unable to construct its own attack.
#
# The guarantee did not change; the ability to demonstrate it did. So the
# forgery is assembled below PyJWT, from `hmac` and base64, where no library
# release can decline to build it. Local had 2.13 and passed; CI resolved 2.14
# and failed — which is also a reminder that without a lockfile, local green
# tests the versions installed once and CI tests the newest allowed.
#
# ## Why well-formedness is proved separately
#
# If the hand-built token were malformed, the API would reject it for being
# malformed and this would pass for the wrong reason. `_hs256_is_valid` checks
# the signature with the same secret, independently of PyJWT.
#
# ## What the 401 does NOT prove — found by breaking it
#
# An earlier draft of this comment said "a 401 below can only mean the algorithm
# was refused". **That was false.** Adding "HS256" to `ALLOWED_ALGORITHMS` left
# all three forgery tests PASSING. The token was still rejected, because PyJWT
# independently refuses to use an EC public key as an HMAC secret.
#
# So the forgery is stopped by TWO layers — our allow-list pin, and PyJWT's
# key-type check — and an end-to-end test cannot tell them apart. It proves the
# outcome (the forgery never authenticates), which is the property users need.
# It does NOT prove our pin, and would keep passing if the pin were deleted as
# long as the library's check held.
#
# Our pin is guarded by `test_allow_list_excludes_symmetric_algorithms`, which
# DID fail when HS256 was added. Two tests, two layers, each stating which one
# it covers — rather than one test claiming both.


def _b64url(raw: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _forge_hs256(claims: dict[str, object], secret: bytes) -> str:
    import hashlib
    import hmac
    import json

    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64url(json.dumps(claims, separators=(",", ":")).encode())
    signing_input = f"{header}.{payload}".encode("ascii")
    signature = hmac.new(secret, signing_input, hashlib.sha256).digest()
    return f"{header}.{payload}.{_b64url(signature)}"


def _hs256_is_valid(token: str, secret: bytes) -> bool:
    import hashlib
    import hmac

    header, payload, signature = token.split(".")
    expected = hmac.new(secret, f"{header}.{payload}".encode("ascii"), hashlib.sha256).digest()
    return hmac.compare_digest(_b64url(expected), signature)


def _confusion_secrets() -> dict[str, bytes]:
    """Every public form of our key an attacker could use as the HMAC secret.

    The PEM is the classic shape of this attack; the JWK and JWKS JSON are what
    an attacker gets by fetching `/.well-known/jwks.json`, which is public by
    design. The original test covered only the last.
    """
    import json

    from cryptography.hazmat.primitives import serialization

    from tests.conftest import _PRIVATE_KEY, JWKS

    pem = _PRIVATE_KEY.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return {
        "public key as PEM": pem,
        "single JWK as JSON": json.dumps(JWKS["keys"][0]).encode(),
        "whole JWKS as JSON": json.dumps(JWKS).encode(),
    }


@pytest.mark.parametrize("shape", sorted(_confusion_secrets()))
def test_algorithm_confusion_downgrade_is_rejected(client: TestClient, shape: str) -> None:
    """A correctly-signed HS256 forgery, keyed with our PUBLIC key, must not
    verify — in any public form an attacker can obtain."""
    secret = _confusion_secrets()[shape]
    token = _forge_hs256({"sub": USER_ID, "exp": int(time.time()) + 3600}, secret)

    assert _hs256_is_valid(token, secret), (
        "the forgery is malformed, so a 401 would prove nothing about algorithm pinning"
    )
    assert client.post("/auth/session", headers=auth(token)).status_code == 401


def test_pyjwt_still_refuses_to_build_this_attack() -> None:
    """A CANARY about the library, not the guarantee about us.

    PyJWT 2.14 refuses to encode a JWK-shaped value as an HMAC secret. That is
    defence in depth we did not write and do not control. If a future release
    relaxes it, this fails — and it SHOULD, because a security defence
    disappearing from a dependency deserves a human looking at it.

    It must not be mistaken for the protection. The API's rejection is proved
    above with a hand-built token that no PyJWT release can refuse to make, so
    this failing never means the API is exposed; it means the library changed.
    """
    import json

    from tests.conftest import JWKS

    with pytest.raises(jwt.exceptions.InvalidKeyError):
        jwt.encode(
            {"sub": USER_ID, "exp": int(time.time()) + 3600},
            json.dumps(JWKS["keys"][0]),
            algorithm="HS256",
        )


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
    """The ONLY test that guards our pin specifically.

    Proved by breaking: with "HS256" added to the allow-list, the end-to-end
    algorithm-confusion tests still passed — PyJWT's own key-type check stopped
    the forgery — and this one failed. So this is not redundant with them; it is
    the half they cannot see. Removing it would leave our defence depending
    entirely on a dependency's behaviour, which a minor release has already
    changed once.
    """
    from carigma_api.auth.jwt_verifier import ALLOWED_ALGORITHMS

    assert "ES256" in ALLOWED_ALGORITHMS
    assert not any(a.upper().startswith("HS") for a in ALLOWED_ALGORITHMS)
    assert "none" not in {a.lower() for a in ALLOWED_ALGORITHMS}


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


# ── A missing service key is not the same failure as an unreachable one ──────


def test_production_refuses_to_start_without_the_service_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The instance check warns and carries on, which is right for a Supabase
    blip. A MISSING key is not a blip: the welcome grant, referral activation,
    unsubscribe tokens and every cron depend on it, and the multi-instance
    CRITICAL never runs. A production build without it would look healthy and
    be broken in five places, so it must not start."""
    import asyncio

    from carigma_api import main as main_mod
    from carigma_api.config import Settings

    prod = Settings(
        environment="production",
        supabase_url="https://example.supabase.co",
        supabase_service_key="",
    )
    monkeypatch.setattr(main_mod, "get_settings", lambda: prod)

    async def start() -> None:
        async with main_mod.lifespan(main_mod.create_app()):
            pass

    with pytest.raises(RuntimeError, match="SUPABASE_SERVICE_KEY"):
        asyncio.run(start())


def test_outside_production_a_missing_key_is_only_a_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CI has no service key and must still boot the app. Refusing there would
    make every test run an outage for a reason that is not a defect."""
    import asyncio

    from carigma_api import main as main_mod
    from carigma_api.config import Settings

    dev = Settings(
        environment="test",
        supabase_url="https://example.supabase.co",
        supabase_service_key="",
    )
    monkeypatch.setattr(main_mod, "get_settings", lambda: dev)

    async def start() -> None:
        async with main_mod.lifespan(main_mod.create_app()):
            pass

    asyncio.run(start())
