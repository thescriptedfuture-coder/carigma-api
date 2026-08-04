"""FastAPI auth dependencies.

Every protected route depends on `CurrentUser`. There is no route-level opt-out
and no "if debug: skip auth" branch — a protected endpoint that forgets its
dependency simply won't compile against the router helpers in routes/.

Three levels:
  CurrentUser  — any verified, signed-in user
  AdminUser    — a verified user on the ADMIN_EMAILS allow-list
  ServiceRole  — the service-role key, server-to-server only, never a browser
"""

from __future__ import annotations

import hmac
import logging
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from carigma_api.auth.jwt_verifier import AuthenticatedUser, InvalidTokenError, JWTVerifier
from carigma_api.config import Settings, get_settings

logger = logging.getLogger(__name__)

# auto_error=False so a missing header produces our own 401 shape rather than
# FastAPI's default 403, which is the wrong status for "no credentials".
_bearer = HTTPBearer(auto_error=False)

# The single generic failure. Never leaks why verification failed.
_UNAUTHENTICATED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Not authenticated.",
    headers={"WWW-Authenticate": "Bearer"},
)
_FORBIDDEN = HTTPException(
    status_code=status.HTTP_403_FORBIDDEN,
    detail="You do not have access to this resource.",
)


def get_verifier(request: Request) -> JWTVerifier:
    """The process-wide verifier, built once at startup (see main.lifespan)."""
    verifier = getattr(request.app.state, "jwt_verifier", None)
    if verifier is None:  # pragma: no cover — startup wiring guarantees this
        raise RuntimeError("JWT verifier not initialised")
    return verifier  # type: ignore[no-any-return]


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    verifier: Annotated[JWTVerifier, Depends(get_verifier)],
) -> AuthenticatedUser:
    """Verify the bearer token. 401 on any failure."""
    if credentials is None or not credentials.credentials:
        raise _UNAUTHENTICATED
    if credentials.scheme.lower() != "bearer":
        raise _UNAUTHENTICATED
    try:
        return verifier.verify(credentials.credentials)
    except InvalidTokenError:
        raise _UNAUTHENTICATED from None


CurrentUser = Annotated[AuthenticatedUser, Depends(get_current_user)]


async def get_admin_user(
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> AuthenticatedUser:
    """Admin gate. Empty allow-list ⇒ nobody is admin (safe default)."""
    allow = settings.admin_email_list
    if not allow or user.email not in allow:
        # Logged so a probe is visible; the caller only ever sees 403.
        logger.warning("Admin access denied for user %s", user.id)
        raise _FORBIDDEN
    return user


AdminUser = Annotated[AuthenticatedUser, Depends(get_admin_user)]


async def require_service_role(
    settings: Annotated[Settings, Depends(get_settings)],
    x_service_key: Annotated[str | None, Header(alias="X-Service-Key")] = None,
) -> None:
    """Server-to-server gate for cron/internal callers.

    The service key is never handed to a browser; it exists so scheduled jobs
    can call the API. Compared with a constant-time check so the endpoint can't
    be used as a timing oracle to recover the key byte by byte.
    """
    expected = settings.supabase_service_key
    if not expected or not x_service_key:
        raise _UNAUTHENTICATED
    if not hmac.compare_digest(x_service_key, expected):
        logger.warning("Service-role access denied: key mismatch")
        raise _UNAUTHENTICATED


ServiceRole = Annotated[None, Depends(require_service_role)]


def assert_owns(user: AuthenticatedUser, owner_id: str) -> None:
    """Guard cross-user access.

    Verifying a token proves *who* the caller is, not *what* they may touch.
    Every route that loads a row by id must call this, or one signed-in user can
    read another's career data by guessing an id (Bible §26.11).
    """
    if user.id != owner_id:
        logger.warning("Ownership check failed: user %s requested %s", user.id, owner_id)
        raise _FORBIDDEN
