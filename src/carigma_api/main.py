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
from carigma_api.routes import admin as admin_routes
from carigma_api.routes import auth as auth_routes
from carigma_api.routes import credits as credits_routes
from carigma_api.routes import health as health_routes
from carigma_api.routes import jobs as jobs_routes
from carigma_api.routes import market as market_routes
from carigma_api.routes import naukri as naukri_routes
from carigma_api.routes import onboarding as onboarding_routes
from carigma_api.routes import payments as payments_routes
from carigma_api.routes import posts as posts_routes
from carigma_api.routes import profile as profile_routes
from carigma_api.routes import profile_updates as profile_update_routes
from carigma_api.routes import referrals as referral_routes
from carigma_api.routes import score as score_routes
from carigma_api.routes import settings as settings_routes
from carigma_api.routes import today as today_routes
from carigma_api.routes import unsubscribe as unsubscribe_routes
from carigma_api.routes import weekly as weekly_routes
from carigma_api.services import instances
from carigma_api.services.repository import service_client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()

    # Fail loudly on a misconfigured auth setup rather than starting an API
    # that cannot verify anybody — in production that would be an open door.
    if settings.is_production and not settings.jwks_url:
        raise RuntimeError(
            "No JWKS endpoint configured. Set SUPABASE_URL (the JWKS URL is "
            "derived from it) or SUPABASE_JWKS_URL explicitly."
        )

    app.state.jwt_verifier = JWTVerifier(settings)

    # The service key, which the instance check below cannot tell apart from a
    # Supabase blip — and they are different failures.
    #
    # `instances.check` warns and carries on, deliberately, because a TRANSIENT
    # failure to reach Supabase must not turn a deploy into an outage. But a
    # MISSING key is not transient. Without it the welcome grant, referral
    # activation, unsubscribe tokens and every cron fail, and the
    # multi-instance CRITICAL below never runs at all — so a production deploy
    # missing this variable would start, look healthy, log one warning, and be
    # broken in five places.
    #
    # Same distinction the JWKS check above already makes: refuse to start a
    # production API that cannot do its job. A failed deploy leaves the previous
    # build serving, which is strictly better than a new one that silently
    # cannot grant credits. Outside production the key is legitimately absent
    # (CI has none) and this stays a warning.
    if settings.is_production and not settings.supabase_service_key:
        raise RuntimeError(
            "SUPABASE_SERVICE_KEY is not set. Production cannot grant credits, activate "
            "referrals, sign unsubscribe links or run the crons without it — refusing to "
            "start rather than serving a build that looks healthy and is not."
        )

    # Count our peers. `services/ratelimit` holds its buckets in process
    # memory, so a second instance silently doubles every ceiling — including
    # the share of the JSearch quota live V1 users are drawing from.
    #
    # This WARNS rather than refuses: a rolling deploy runs old and new
    # together for a minute, and turning every routine deploy into an outage
    # is a worse failure than the one being prevented. The CRITICAL is the
    # mechanism — it makes scaling a decision rather than something that
    # happened. Never raises; a monitoring gap must not be an outage.
    try:
        app.state.instances = instances.check(
            service_client(settings), version=settings.environment
        )
    except Exception:
        # Reached with a key present only when Supabase is unreachable, which is
        # the transient case this is right to tolerate. Without a key it is
        # reached only outside production — see the refusal above.
        logger.warning("instance check skipped", exc_info=True)
        app.state.instances = None

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
    app.include_router(score_routes.router)
    app.include_router(weekly_routes.router)
    app.include_router(posts_routes.router)
    app.include_router(profile_update_routes.router)
    app.include_router(settings_routes.router)
    app.include_router(payments_routes.router)
    app.include_router(admin_routes.router)
    app.include_router(admin_routes.request_router)
    app.include_router(market_routes.admin_router)
    app.include_router(market_routes.router)
    app.include_router(naukri_routes.router)
    app.include_router(jobs_routes.router)
    app.include_router(credits_routes.router)
    app.include_router(profile_routes.router)
    app.include_router(today_routes.router)
    app.include_router(referral_routes.router)
    app.include_router(onboarding_routes.router)
    app.include_router(unsubscribe_routes.router)
    return app


app = create_app()
