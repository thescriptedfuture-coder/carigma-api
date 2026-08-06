"""Supabase JWT verification.

This is the door to users' career data. The rules it enforces:

- **Asymmetric verification only.** Supabase signs with an ECC P-256 key
  (ES256); we fetch the *public* key from JWKS. The legacy HS256 shared secret
  is deliberately unsupported: an HMAC secret verifies **and mints**, so holding
  one would mean this service could forge a token for any user. A verifier
  should only be able to verify.
- The signature is always checked. There is no code path that decodes a token
  without verifying it, and no "trust the client" fallback.
- `exp`, `aud` and `iss` are validated, not merely parsed.
- `alg` is pinned to an allow-list, so a token cannot talk us into `none` or
  downgrade us to a symmetric algorithm.
- Every failure returns the *same* generic message to the caller. Telling an
  attacker whether a token was expired vs. malformed vs. wrongly signed is free
  reconnaissance; the specific reason is logged server-side only.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

import httpx
import jwt
from jwt import PyJWKClient

from carigma_api.config import Settings

logger = logging.getLogger(__name__)

# Supabase signs with ECC P-256. RS256 is accepted because Supabase's own key
# rotation can move a project to RSA; HS256 and friends are NOT accepted — see
# the module docstring.
ALLOWED_ALGORITHMS = ("ES256", "RS256")

# Tolerance for clock skew between Supabase's servers and ours, in seconds.
#
# This is not theoretical: the first end-to-end test against the live project
# rejected a freshly-minted, perfectly valid token with
# `ImmatureSignatureError: The token is not yet valid (iat)` — Supabase's clock
# was a moment ahead of ours. Without leeway that is a real availability bug:
# every sign-in fails whenever our clock drifts behind theirs.
#
# 60s is the conventional allowance. It applies to `iat`/`nbf` (tolerating a
# fast issuer) and to `exp` (tolerating a slow one). Extending a ~1-hour token's
# life by up to a minute is a negligible cost against refusing valid users.
CLOCK_SKEW_LEEWAY_SECONDS = 60


class InvalidTokenError(Exception):
    """Raised for every verification failure.

    Deliberately carries no detail for the caller — the reason is logged, not
    returned. See the module docstring.
    """


@dataclass(frozen=True)
class AuthenticatedUser:
    """The verified caller. Built only from *verified* claims."""

    id: str
    email: str
    role: str
    claims: dict[str, Any]


class _JWKSCache:
    """Thread-safe JWKS client with an explicit TTL.

    PyJWKClient caches keys itself; the TTL on top means a rotated signing key
    is picked up without a redeploy.
    """

    def __init__(self, url: str, ttl_seconds: int = 600) -> None:
        self._url = url
        self._ttl = ttl_seconds
        self._client: PyJWKClient | None = None
        self._fetched_at: float = 0.0
        self._lock = threading.Lock()

    def get(self) -> PyJWKClient:
        with self._lock:
            expired = (time.monotonic() - self._fetched_at) > self._ttl
            if self._client is None or expired:
                self._client = PyJWKClient(self._url, cache_keys=True)
                self._fetched_at = time.monotonic()
            return self._client

    def invalidate(self) -> None:
        """Force a refetch — used once on a verification miss, so a key rotated
        inside the TTL window doesn't cause a wave of spurious 401s."""
        with self._lock:
            self._client = None
            self._fetched_at = 0.0


class JWTVerifier:
    """Verifies Supabase-issued access tokens against the project's JWKS."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._jwks: _JWKSCache | None = None
        if settings.jwks_url:
            self._jwks = _JWKSCache(settings.jwks_url, settings.jwks_cache_ttl_seconds)

    @property
    def _issuer(self) -> str | None:
        if not self._settings.supabase_url:
            return None
        return f"{self._settings.supabase_url.rstrip('/')}/auth/v1"

    def verify(self, token: str) -> AuthenticatedUser:
        """Verify a bearer token and return the caller.

        Raises InvalidTokenError on any failure.
        """
        if not token or not token.strip():
            raise InvalidTokenError

        if self._jwks is None:
            logger.error("JWT rejected: no JWKS URL configured")
            raise InvalidTokenError

        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            logger.warning("JWT rejected: unreadable header (%s)", exc)
            raise InvalidTokenError from exc

        # Pin the algorithm from OUR allow-list, never from the token. An
        # attacker who controls `alg` would otherwise try `none`, or present an
        # HS256 token hoping we verify it with a public key as the HMAC secret.
        alg = header.get("alg")
        if alg not in ALLOWED_ALGORITHMS:
            logger.warning("JWT rejected: disallowed alg %r", alg)
            raise InvalidTokenError

        claims = self._decode_with_retry(token, alg)
        return self._build_user(claims)

    def _decode_with_retry(self, token: str, alg: str) -> dict[str, Any]:
        """Decode, refetching JWKS once if the key isn't known yet.

        A key rotated inside the cache TTL would otherwise 401 every request
        until the TTL expired.
        """
        if self._jwks is None:  # pragma: no cover — verify() guards this
            raise InvalidTokenError
        try:
            return self._decode(token, alg)
        except (jwt.PyJWKClientError, jwt.exceptions.InvalidKeyError):
            logger.info("Signing key not found in cached JWKS; refetching once")
            self._jwks.invalidate()
        except (jwt.PyJWTError, httpx.HTTPError) as exc:
            logger.warning("JWT rejected: verification failed (%s)", exc)
            raise InvalidTokenError from exc

        try:
            return self._decode(token, alg)
        except (jwt.PyJWTError, httpx.HTTPError) as exc:
            logger.warning("JWT rejected after JWKS refetch (%s)", exc)
            raise InvalidTokenError from exc

    def _decode(self, token: str, alg: str) -> dict[str, Any]:
        # A real check, not an assert: asserts are stripped under `python -O`,
        # and this is a security path.
        if self._jwks is None:  # pragma: no cover — verify() guards this
            raise InvalidTokenError
        signing_key = self._jwks.get().get_signing_key_from_jwt(token)
        decoded: dict[str, Any] = jwt.decode(
            token,
            key=signing_key.key,
            algorithms=[alg],
            audience=self._settings.supabase_jwt_audience or None,
            issuer=self._issuer,
            leeway=CLOCK_SKEW_LEEWAY_SECONDS,
            options={
                "verify_signature": True,
                "verify_exp": True,
                "verify_aud": bool(self._settings.supabase_jwt_audience),
                "verify_iss": bool(self._issuer),
                "require": ["exp", "sub"],
            },
        )
        return decoded

    def _build_user(self, claims: dict[str, Any]) -> AuthenticatedUser:
        user_id = claims.get("sub")
        if not user_id or not isinstance(user_id, str):
            logger.warning("JWT rejected: missing or non-string sub claim")
            raise InvalidTokenError

        role = claims.get("role") or "authenticated"
        # An anon-key token is not a signed-in user. Supabase issues these for
        # unauthenticated clients; accepting one would open the front door.
        if role == "anon":
            logger.warning("JWT rejected: anon role presented as a user token")
            raise InvalidTokenError

        email = claims.get("email") or ""
        return AuthenticatedUser(
            id=user_id,
            email=str(email).strip().lower(),
            role=str(role),
            claims=claims,
        )
