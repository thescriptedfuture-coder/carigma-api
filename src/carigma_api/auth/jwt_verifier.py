"""Supabase JWT verification.

This is the door to users' career data. The rules it enforces:

- The signature is **always** verified. There is no code path that decodes a
  token without checking it, and no "trust the client" fallback.
- `exp`, `aud` and `iss` are validated, not merely parsed.
- `alg` is pinned to an allow-list, so a token cannot talk us into `none` or
  downgrade an asymmetric project to a symmetric secret we happen to hold.
- Every failure returns the *same* generic message to the caller. Telling an
  attacker whether a token was expired vs. malformed vs. wrongly signed is free
  reconnaissance; the specific reason is logged server-side only.

Two Supabase generations are supported: legacy projects that sign with a shared
HS256 secret, and current projects that publish asymmetric keys via JWKS.
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

_SYMMETRIC_ALGS = ["HS256"]
_ASYMMETRIC_ALGS = ["RS256", "ES256"]


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

    @property
    def is_anonymous(self) -> bool:
        return self.role == "anon"


class _JWKSCache:
    """Thread-safe wrapper around PyJWKClient.

    PyJWKClient caches keys itself, but we add an explicit TTL so a rotated
    signing key is picked up without a redeploy.
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


class JWTVerifier:
    """Verifies Supabase-issued access tokens."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._jwks: _JWKSCache | None = None
        if settings.jwks_url:
            self._jwks = _JWKSCache(settings.jwks_url)

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

        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            logger.warning("JWT rejected: unreadable header (%s)", exc)
            raise InvalidTokenError from exc

        alg = header.get("alg")
        # Pin the algorithm. An attacker who controls `alg` can otherwise try
        # `none`, or coax an RS256 project into verifying with a public key
        # treated as an HMAC secret.
        if alg in _ASYMMETRIC_ALGS:
            claims = self._decode_asymmetric(token, alg)
        elif alg in _SYMMETRIC_ALGS:
            claims = self._decode_symmetric(token)
        else:
            logger.warning("JWT rejected: disallowed alg %r", alg)
            raise InvalidTokenError

        return self._build_user(claims)

    # ── decoders ───────────────────────────────────────────────────────────

    def _decode_asymmetric(self, token: str, alg: str) -> dict[str, Any]:
        if self._jwks is None:
            logger.error("JWT rejected: asymmetric token but no JWKS URL configured")
            raise InvalidTokenError
        try:
            signing_key = self._jwks.get().get_signing_key_from_jwt(token)
            return self._decode(token, signing_key.key, [alg])
        except (jwt.PyJWTError, httpx.HTTPError) as exc:
            logger.warning("JWT rejected: asymmetric verification failed (%s)", exc)
            raise InvalidTokenError from exc

    def _decode_symmetric(self, token: str) -> dict[str, Any]:
        secret = self._settings.supabase_jwt_secret
        if not secret:
            logger.error("JWT rejected: HS256 token but SUPABASE_JWT_SECRET is unset")
            raise InvalidTokenError
        try:
            return self._decode(token, secret, _SYMMETRIC_ALGS)
        except jwt.PyJWTError as exc:
            logger.warning("JWT rejected: symmetric verification failed (%s)", exc)
            raise InvalidTokenError from exc

    def _decode(self, token: str, key: Any, algorithms: list[str]) -> dict[str, Any]:
        decoded: dict[str, Any] = jwt.decode(
            token,
            key=key,
            algorithms=algorithms,
            audience=self._settings.supabase_jwt_audience or None,
            issuer=self._issuer,
            options={
                "verify_signature": True,
                "verify_exp": True,
                "verify_aud": bool(self._settings.supabase_jwt_audience),
                "verify_iss": bool(self._issuer),
                "require": ["exp", "sub"],
            },
        )
        return decoded

    # ── claim mapping ──────────────────────────────────────────────────────

    def _build_user(self, claims: dict[str, Any]) -> AuthenticatedUser:
        user_id = claims.get("sub")
        if not user_id or not isinstance(user_id, str):
            logger.warning("JWT rejected: missing or non-string sub claim")
            raise InvalidTokenError

        role = claims.get("role") or "authenticated"
        # An anon-key token is not a signed-in user. Supabase issues these for
        # unauthenticated clients; accepting one as a user would let anybody
        # through the front door.
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
