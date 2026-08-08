"""Shared request guards for endpoints that spend credits.

Both guards exist because the alternative is remembering to apply them. Any
endpoint that can charge should call `guard_spend` first, and every one of them
gets the same 429 shape, the same replay semantics, and the same error copy.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException, Request, status

from carigma_api.services.ratelimit import client_ip, limiter
from carigma_api.services.run_store import IdempotencyConflict, replay_or_none, replay_payload
from carigma_api.services.runs import AgentRun

logger = logging.getLogger(__name__)


def enforce_agent_rate_limit(user_id: str, agent: str) -> None:
    """Limiter 1. Raises 429 with a real retry time, and charges nothing."""
    decision = limiter.check_agent_run(user_id, agent)
    if decision.allowed:
        return
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=decision.as_dict(),
        # Standard header as well as the body, so a generic HTTP client can
        # back off correctly without parsing our JSON.
        headers={
            "Retry-After": str(decision.retry_after_seconds),
            "X-Error-Code": "rate_limited",
        },
    )


def enforce_public_rate_limit(request: Request) -> None:
    """Limiter 2. For endpoints reachable without a token."""
    ip = client_ip({k.lower(): v for k, v in request.headers.items()})
    decision = limiter.check_public(ip)
    if decision.allowed:
        return
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=decision.as_dict(),
        headers={
            "Retry-After": str(decision.retry_after_seconds),
            "X-Error-Code": "rate_limited",
        },
    )


def idempotency_key(request: Request) -> str | None:
    key = (request.headers.get("Idempotency-Key") or "").strip()
    return key or None


def replay_if_seen(
    store: Any, user_id: str, request: Request, *, agent: str
) -> dict[str, Any] | None:
    """Return the original response for a repeated key, or None to proceed.

    Runs BEFORE the rate limiter and before the credit check: a replay is not a
    new request. Rate-limiting or re-charging a retry of work already done
    would punish the client for the network being unreliable.
    """
    key = idempotency_key(request)
    if not key:
        return None
    try:
        previous: AgentRun | None = replay_or_none(store, user_id, key, agent=agent)
    except IdempotencyConflict as exc:
        logger.info("idempotency conflict: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "idempotency_conflict",
                "message": (
                    "That request key was already used for something else. "
                    "Use a new key, or retry the original request."
                ),
            },
        ) from exc
    return replay_payload(previous) if previous else None
