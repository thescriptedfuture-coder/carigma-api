"""Profile Analyst endpoints — the first fully extracted agent.

Proves the whole P2 spine end-to-end: verified JWT → stateless service →
run protocol → credit rule → persisted result.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from carigma_api.auth.dependencies import CurrentUser
from carigma_api.config import Settings, get_settings
from carigma_api.routes._guards import enforce_agent_rate_limit, idempotency_key, replay_if_seen
from carigma_api.services import credits as credits_service
from carigma_api.services import runs as runs_service
from carigma_api.services.agents import profile_analyst
from carigma_api.services.credits import InsufficientCredits
from carigma_api.services.repository import (
    ProfileRepository,
    ScoreRepository,
    SupabaseCreditStore,
    user_client,
)
from carigma_api.services.run_store import SupabaseRunStore
from carigma_api.services.runs import RunReporter

logger = logging.getLogger(__name__)

router = APIRouter(tags=["score"])


class ComputeScoreRequest(BaseModel):
    # The FIRST score is free — it is the product's promise, and charging for it
    # would gate the one thing that proves the value (contract §2).
    onboarding: bool = False


class ApplyFixRequest(BaseModel):
    fix_key: str = Field(min_length=1)
    rewrite: str = Field(min_length=1)


def _deps(request: Request, user: Any, settings: Settings) -> tuple[Any, Any, Any, Any]:
    """Build per-request, per-user Supabase-backed collaborators.

    Everything that needs the caller's own client is built HERE, in one place,
    so a test that substitutes this substitutes all of it. An extra
    `user_client(...)` call elsewhere in the handler would slip past that.
    """
    token = (request.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
    client = user_client(settings, token)
    return (
        ProfileRepository(client),
        ScoreRepository(client),
        SupabaseCreditStore(client),
        SupabaseRunStore(client),
    )


@router.post("/score/compute", status_code=status.HTTP_200_OK)
async def compute_score(
    body: ComputeScoreRequest,
    request: Request,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """Run the Profile Analyst.

    Synchronous because the client waits on this one (it is the decode moment).
    Longer agents use POST /agents/{agent}/run + SSE.
    """
    profiles, scores, credit_store, run_store = _deps(request, user, settings)

    # A repeated Idempotency-Key is answered from the original run BEFORE the
    # rate limiter and the credit check — a retry of work already done is not a
    # new request, and must be neither throttled nor charged again.
    if (replayed := replay_if_seen(run_store, user.id, request, agent="profile")) is not None:
        return replayed

    # Limiter 1 (§21 Q7). The credit gate below stops cost; this stops abuse.
    enforce_agent_rate_limit(user.id, "profile")

    action = "onboarding_score" if body.onboarding else "run_profile"

    try:
        credits_service.check_affordable(
            credit_store, user.id, action, enforced=settings.credits_enforced
        )
    except credits_service.CreditsUnavailable as exc:
        # We could not read the balance, so we cannot account for this run.
        # Refusing is not "charging on failure" — nothing is charged and no
        # work starts. Before this, an unreadable balance ran the work FREE.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="We could not check your credits just now. Try again in a moment.",
            headers={"X-Error-Code": "credits_unavailable"},
        ) from exc
    except InsufficientCredits as exc:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=(
                f"You need {exc.required} credits to run the Profile Analyst. "
                f"You have {exc.balance}."
            ),
            headers={"X-Error-Code": "insufficient_credits"},
        ) from exc

    profile = profiles.load(user.id)
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Add your profile details first — the analyst needs something to read.",
        )

    run = runs_service.new_run(user.id, "profile")
    if key := idempotency_key(request):
        run_store.remember_idempotency(user.id, key, run.id)

    def work(reporter: RunReporter) -> dict[str, Any]:
        reporter.step("analyse", "Reading your profile")
        result = profile_analyst.run(profile, api_key=settings.anthropic_api_key)
        reporter.complete_step()
        return result

    settled = await runs_service.execute(
        run, work, credit_store=credit_store, action=action, run_store=run_store
    )

    if settled.status is runs_service.RunStatus.FAILED:
        # No credits were charged — see services/runs.execute.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_friendly(settled.error),
            headers={"X-Error-Code": "upstream_unavailable"},
        )

    # Record the score only on a real result — never persist a fabricated one.
    if settled.status is runs_service.RunStatus.SUCCEEDED:
        try:
            scores.record(user.id, int(settled.result["profileScore"]))
        except Exception:
            logger.exception("Score history write failed for %s", user.id)

    return {
        "result": settled.result,
        "credits": settled.credits.as_dict() if settled.credits else None,
        "provenance": {
            "agent_label": "Profile Analyst",
            "ran_at": settled.started_at.isoformat(),
        },
    }


@router.post("/score/fix")
async def apply_fix(
    body: ApplyFixRequest,
    request: Request,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """Apply an analyst rewrite to the stored profile.

    This WRITES the rewrite before returning. That is the whole fix for V1's
    "score never improves" bug: a flag alone left the next run scoring stale
    data. Free — the user is accepting our suggestion, not buying anything.
    """
    profiles, _scores, _credits, _runs = _deps(request, user, settings)

    profile = profiles.load(user.id)
    if not profile:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found.")

    if body.fix_key not in profile_analyst.SCORE_FIX_FIELDS:
        raise HTTPException(
            # Literal 422: Starlette renamed the constant, and pinning to either
            # spelling couples us to a version for no benefit.
            status_code=422,
            detail=(
                f"'{body.fix_key}' isn't an applyable fix. "
                f"Applyable: {', '.join(sorted(profile_analyst.SCORE_FIX_FIELDS))}."
            ),
        )

    updated = profile_analyst.apply_score_fix(profile, body.fix_key, body.rewrite)
    saved = profiles.save(user.id, updated)

    field = profile_analyst.SCORE_FIX_FIELDS[body.fix_key]
    return {"applied": True, "field": field, "profile": saved}


@router.get("/score/history")
async def score_history(
    request: Request,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
    limit: int = 60,
) -> dict[str, Any]:
    _profiles, scores, _credits, _runs = _deps(request, user, settings)
    return {"items": scores.history(user.id, min(max(limit, 1), 200))}


def _friendly(error: str | None) -> str:
    """Map a failure to plain language.

    Ported from V1 `errors.friendly_message`. Users never see a raw exception,
    and error copy carries no emoji.
    """
    low = (error or "").lower()

    def has(*words: str) -> bool:
        return any(w in low for w in words)

    if has("overloaded", "rate limit", "rate_limit", "429", "too many requests"):
        return "Things are busy right now. Please wait a few seconds and try again."
    if has("timed out", "timeout", "connection", "network", "502", "503", "504"):
        return "We couldn't reach the server. Check your connection and try again in a moment."
    if has("api key", "authentication", "401"):
        return "That feature isn't fully switched on yet — our team has been notified."
    return "We couldn't complete that just now. Please try again in a moment."
