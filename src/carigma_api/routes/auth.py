"""Auth routes.

`POST /auth/session` is the roadmap's Part 2 entry point: React holds the
Supabase session, sends the JWT, and the API returns the verified user context.

The probe routes exist so the auth layer is exercisable end-to-end (and by the
test suite) before any business logic lands in P2.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from carigma_api.auth.dependencies import AdminUser, CurrentUser, ServiceRole
from carigma_api.auth.jwt_verifier import AuthenticatedUser
from carigma_api.config import Settings, get_settings

router = APIRouter(prefix="/auth", tags=["auth"])


class SessionResponse(BaseModel):
    """The verified caller. Built from verified claims only — never from
    anything the client asserts about itself."""

    user_id: str
    email: str
    role: str
    is_admin: bool


def _session_for(user: AuthenticatedUser, settings: Settings) -> SessionResponse:
    allow = settings.admin_email_list
    return SessionResponse(
        user_id=user.id,
        email=user.email,
        role=user.role,
        is_admin=bool(allow and user.email in allow),
    )


@router.post("/session", response_model=SessionResponse)
async def create_session(
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> SessionResponse:
    """Verify the Supabase JWT and return user context."""
    return _session_for(user, settings)


@router.get("/me", response_model=SessionResponse)
async def me(
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> SessionResponse:
    """Current user — the canonical 'is my token still good?' probe."""
    return _session_for(user, settings)


@router.get("/admin-probe")
async def admin_probe(user: AdminUser) -> dict[str, str]:
    """Proves the admin gate. Non-admins get 403, never 200."""
    return {"status": "ok", "user_id": user.id}


@router.get("/service-probe")
async def service_probe(_: ServiceRole) -> dict[str, str]:
    """Proves the service-role gate (cron/server-to-server only)."""
    return {"status": "ok"}
