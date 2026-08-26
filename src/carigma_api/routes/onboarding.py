"""Onboarding — the path V2 did not have. Brief 3a.

    GET  /onboarding            free — where am I
    POST /onboarding/extract    free — a file or pasted text becomes a profile
    POST /onboarding/complete   free — targets, cadence, platforms; sets onboarded

The score run in the middle is `POST /score/compute` with `onboarding: true`,
which already exists and is already free (`onboarding_score`).

## What was missing, and how it hid

V2 had no upload route, no extraction, and nothing anywhere that wrote
`onboarded`. `DecodePage` made no API calls at all — it is a beautiful
animation over no data. A stranger after cutover would sign up, watch the most
polished screen in the product do nothing, and arrive with an empty profile
that every other feature reads.

It hid the way the other seven did: the surface renders perfectly.

## Extraction is FREE, and that is the whole strategy

The rewritten headline in beat 2 is the conversion event, and it has to be
*their* headline. A demo would be cheaper and would convert nobody, because
the thing being sold is that we read their actual profile. Charging for the
step that proves the product works would be charging for the sales pitch.

## Nothing here fabricates a profile

An unreadable file returns a sentence and the paste option — never a
placeholder profile, and never a partially-invented one. `clean()` drops empty
strings rather than writing them, so a thin re-upload cannot erase fields the
user already has.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from pydantic import BaseModel, Field

from carigma_api.auth.dependencies import CurrentUser
from carigma_api.config import Settings, get_settings
from carigma_api.services import extraction, onboarding
from carigma_api.services.ai import call_claude_json
from carigma_api.services.referral_store import SupabaseReferralStore
from carigma_api.services.referrals import activation_from_profile_upload
from carigma_api.services.repository import (
    ProfileRepository,
    service_client,
    user_client,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["onboarding"])


class CompleteRequest(BaseModel):
    target_roles: str = Field(min_length=1, max_length=200)
    cadence: int | None = None
    platforms: list[str] | None = None
    location: str | None = Field(default=None, max_length=120)


def _profiles(request: Request, settings: Settings) -> ProfileRepository:
    token = (request.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
    return ProfileRepository(user_client(settings, token))


def _unavailable(what: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={"error": "read_failed", "message": f"We couldn't {what} just now."},
    )


@router.get("/onboarding")
def get_onboarding(
    request: Request,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """Where this user is, derived from the profile rather than stored.

    A stored step would be a second source of truth: close the tab after the
    extraction and it disagrees with a row that plainly has a headline in it.
    """
    profiles = _profiles(request, settings)
    try:
        profile = profiles.load(user.id)
    except Exception as exc:
        logger.exception("could not read the profile for %s", user.id)
        raise _unavailable("load your setup") from exc

    scored = bool(str(profile.get("linkedinHeadline") or "").strip()) and bool(
        profile.get("onboarded")
    )
    step = onboarding.step_for(profile, scored=scored)

    return {
        "step": str(step),
        "onboarded": bool(profile.get("onboarded")),
        "has_profile": onboarding.has_profile_text(profile),
        # Offered so the client renders the real options rather than its own
        # copy of them. V1's cadence list drifted from the database twice.
        "cadences": list(onboarding.CADENCES),
        "default_cadence": onboarding.DEFAULT_CADENCE,
        "platforms": list(onboarding.PLATFORMS),
        # What we already know, so the targets step is pre-filled rather than
        # asking again for something the extraction just found.
        "suggested": {
            "target_roles": profile.get("targetRoles") or "",
            "location": profile.get("location") or "",
        },
    }


@router.post("/onboarding/extract")
async def extract_profile(
    request: Request,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
    file: Annotated[UploadFile | None, File()] = None,
    text: Annotated[str | None, Form()] = None,
) -> dict[str, Any]:
    """A PDF, a .docx or pasted text becomes profile fields. FREE.

    The paste path is a first-class input, not a fallback: a scanned resume
    produces no text at all, and a LinkedIn export is not something everyone
    can make on a phone.
    """
    profiles = _profiles(request, settings)

    if file is None and not (text or "").strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "nothing_to_read",
                "message": "Upload a resume or LinkedIn PDF, or paste your profile text.",
            },
        )

    try:
        if file is not None:
            raw = await file.read()
            source = extraction.text_from_upload(raw, file.filename or "")
        else:
            source = (text or "").strip()
        fields = extraction.parse_profile_text(
            source,
            extractor=lambda system, user_prompt, max_tokens: call_claude_json(
                system, user_prompt, max_tokens, api_key=settings.anthropic_api_key
            ),
        )
    except extraction.Unreadable as exc:
        # Not a 500 and not a dead end. The sentence names the next thing to
        # try, and the client keeps the paste box open.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "unreadable", "message": str(exc), "can_paste": True},
        ) from exc
    except Exception as exc:
        logger.exception("profile extraction failed for %s", user.id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "extract_failed",
                "message": "We couldn't read your profile just now. Nothing was saved.",
                "can_paste": True,
            },
        ) from exc

    if not fields:
        # The model answered and found nothing usable. Distinct from a read
        # failure, and the advice is different.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "nothing_found",
                "message": (
                    "We read the file but couldn't find profile details in it. "
                    "Try your LinkedIn PDF export, or paste your profile text."
                ),
                "can_paste": True,
            },
        )

    try:
        profiles.save(user.id, fields)
    except Exception as exc:
        logger.exception("could not save the extracted profile for %s", user.id)
        raise _unavailable("save your profile") from exc

    return {
        # The names of what landed, so the client can say "we found your
        # headline and 14 skills" instead of a spinner that ends silently.
        "extracted": sorted(fields),
        "field_count": len(fields),
        "next": str(onboarding.Step.DECODING),
    }


@router.post("/onboarding/complete")
def complete_onboarding(
    body: CompleteRequest,
    request: Request,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """Targets, cadence and platform preference. Writes `onboarded`.

    This is the only place in V2 that sets it, and the only trigger for a
    referral activation — which is why the activation lives here rather than
    behind a route of its own.
    """
    profiles = _profiles(request, settings)

    try:
        patch = onboarding.completion(
            target_roles=body.target_roles,
            cadence=body.cadence,
            platforms=body.platforms,
            location=body.location,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "no_target", "message": str(exc)},
        ) from exc

    try:
        profiles.save(user.id, patch)
        # Read BACK. `profile_to_db` raising on an unknown key is the guard,
        # but a save that silently wrote nothing would leave a user who has
        # finished setup being asked to finish setup on every load.
        saved = profiles.load(user.id)
    except Exception as exc:
        logger.exception("could not complete onboarding for %s", user.id)
        raise _unavailable("save your setup") from exc

    if not saved.get("onboarded"):
        raise _unavailable("save your setup")

    return {
        # From the ROW, not a literal. We just checked it above, so a literal
        # would be true — but the same shape one route over was echoing a
        # decision instead of reporting one, and the difference only shows up
        # on the day the write starts failing quietly.
        "onboarded": bool(saved.get("onboarded")),
        "step": str(onboarding.Step.DONE),
        "target_roles": saved.get("targetRoles"),
        "cadence": saved.get("cadence"),
        "platforms": saved.get("platforms") or [],
        "referral": _activate_referral(user.id, settings),
    }


def _activate_referral(user_id: str, settings: Settings) -> dict[str, Any] | None:
    """The referral programme's single trigger — a completed profile.

    Service-role, because `referrals` has RLS with no policy and deliberately
    no RPC: a function `authenticated` could execute would be a browser-callable
    route to 20 free credits, routing straight around the `Activation` type.
    This is a system action after an upload, which is the category the service
    key is for.

    **Never raises.** Most users were not referred, and no user should lose
    their finished setup because the referral programme attached to it failed.
    """
    try:
        client = service_client(settings)
    except Exception:
        logger.warning("no service client; referral activation skipped for %s", user_id)
        return None

    try:
        store = SupabaseReferralStore(client)
        grant = store.activate(activation_from_profile_upload(user_id))
    except Exception:
        logger.exception("referral activation failed for %s", user_id)
        return None

    if grant is None:
        return None
    return {"credits": grant.referred_credits, "reason": "Welcome bonus from a referral"}


__all__ = ["router"]
