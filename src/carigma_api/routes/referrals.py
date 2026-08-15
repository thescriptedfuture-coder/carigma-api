"""Referral endpoints. Contract §20.

    GET  /referrals          free — your code and what it has earned
    POST /referrals/claim    free — record who referred you
    GET  /referrals/{code}   free, PUBLIC — what the landing page shows

**There is no route that grants credits, and there must never be one.**

The reward is triggered by a profile upload, not by a request. `activate` takes
an `Activation`, which only `activation_from_profile_upload` constructs, so
there is no signature a handler here could call it with even by accident. A
route that grants would be a second door, and the whole point of the type is
that there is one.

`POST /referrals/claim` records a PENDING attribution and moves nothing. That
distinction is the design: signing up through a link is attribution, uploading
a profile is activation, and only the second is worth 50 credits.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from carigma_api.auth.dependencies import CurrentUser
from carigma_api.config import Settings, get_settings
from carigma_api.routes._guards import enforce_public_rate_limit
from carigma_api.services.referral_store import SupabaseReferralStore, summary
from carigma_api.services.referrals import (
    CODE_LENGTH,
    REFERRED_CREDITS,
    AlreadyReferred,
    CapReached,
    SelfReferral,
    share_payload,
)
from carigma_api.services.repository import user_client

logger = logging.getLogger(__name__)

router = APIRouter(tags=["referrals"])


class ClaimRequest(BaseModel):
    code: str = Field(min_length=6, max_length=CODE_LENGTH + 4)


def _store(request: Request, settings: Settings) -> SupabaseReferralStore:
    token = (request.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
    return SupabaseReferralStore(user_client(settings, token))


def _unavailable() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={"error": "read_failed", "message": "We couldn't load your referrals just now."},
    )


@router.get("/referrals")
def get_referrals(
    request: Request,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """Your code, your link, and what it has actually earned.

    The code is minted on first read rather than at signup — a row for every
    user who never shares is a row that never needed to exist.
    """
    store = _store(request, settings)
    try:
        code = store.code_for(user.id)
        counts = store.summary_counts()
    except Exception as exc:
        logger.exception("could not read referrals for %s", user.id)
        raise _unavailable() from exc

    return summary(
        code=code,
        counts=counts,
        share=share_payload(code, base_url=settings.app_url),
    )


@router.post("/referrals/claim")
def claim_referral(
    body: ClaimRequest,
    request: Request,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """Record who referred you. Grants nothing.

    Called once, when a user who arrived through a link finishes signing up.
    Every refusal here is a plain sentence rather than a constraint violation,
    and none of them is a failure the user caused — an expired code and a full
    one are both the programme working.
    """
    store = _store(request, settings)
    try:
        row = store.claim(code=body.code.strip().lower())
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "unknown_code", "message": "That link isn't valid."},
        ) from exc
    except SelfReferral as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "self_referral", "message": str(exc)},
        ) from exc
    except AlreadyReferred as exc:
        # Not an error the user can act on, and not worth a scary word: they
        # already have whatever the first link gave them.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "already_referred", "message": str(exc)},
        ) from exc
    except CapReached as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "cap_reached", "message": str(exc)},
        ) from exc
    except Exception as exc:
        logger.exception("could not claim a referral for %s", user.id)
        raise _unavailable() from exc

    return {
        "state": row["state"],
        # Said explicitly, because the credits do NOT arrive now and a silent
        # success would read as though they had.
        "credits_now": 0,
        "credits_on_upload": REFERRED_CREDITS,
        "message": (f"You're in. {REFERRED_CREDITS} credits land when you upload your profile."),
    }


@router.get("/referrals/{code}")
def describe_code(
    code: str,
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """What the landing page says. PUBLIC — the visitor has no account yet.

    Returns whether the code is real and what the visitor gets, and **nothing
    about the referrer.** Their name, email and id are not the visitor's
    business, and an endpoint that confirmed "this is Ravi's code" to anyone
    holding a string would be a lookup service for our user list.
    """
    enforce_public_rate_limit(request)
    store = SupabaseReferralStore(user_client(settings, ""))

    try:
        known = store.code_exists(code.strip().lower())
    except Exception:
        # A read failure must not render "invalid link" on a real one. The
        # visitor is told the truth: we could not check.
        logger.exception("could not resolve referral code")
        raise _unavailable() from None

    return {
        "valid": known,
        "credits": REFERRED_CREDITS if known else 0,
        "headline": (
            f"You've been given {REFERRED_CREDITS} free credits"
            if known
            else "That link isn't valid"
        ),
        "sub": (
            "Upload your profile and they're yours."
            if known
            else "Ask for a new one — links don't expire, so this one was probably mistyped."
        ),
    }


__all__ = ["router"]
