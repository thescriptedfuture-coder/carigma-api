"""Health checks — the only unauthenticated routes in the service."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Liveness probe. Deliberately leaks no configuration detail."""
    return HealthResponse(status="ok", service="carigma-api", version="0.1.0")
