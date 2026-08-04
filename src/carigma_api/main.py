"""Carigma V2 API — application entrypoint.

P1 scope: scaffolding, auth, health. No business logic is ported here yet —
that is P2 (see PROJECT_STATUS_V2.md).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from carigma_api.auth.jwt_verifier import JWTVerifier
from carigma_api.config import get_settings
from carigma_api.routes import auth as auth_routes
from carigma_api.routes import health as health_routes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()

    # Fail loudly on a misconfigured auth setup rather than starting an API
    # that cannot verify anybody — in production that would be an open door.
    if settings.is_production and not (settings.supabase_jwt_secret or settings.jwks_url):
        raise RuntimeError(
            "No JWT verification method configured. Set SUPABASE_JWT_SECRET "
            "(legacy HS256) or SUPABASE_URL/SUPABASE_JWKS_URL (asymmetric)."
        )

    app.state.jwt_verifier = JWTVerifier(settings)
    logger.info("carigma-api started (environment=%s)", settings.environment)
    yield


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="Carigma API",
        version="0.1.0",
        description="Carigma V2 — career intelligence API.",
        lifespan=lifespan,
        # Interactive docs are useful in development and are noise (plus a free
        # endpoint map) in production.
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if settings.is_production else "/openapi.json",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
    )

    app.include_router(health_routes.router)
    app.include_router(auth_routes.router)
    return app


app = create_app()
