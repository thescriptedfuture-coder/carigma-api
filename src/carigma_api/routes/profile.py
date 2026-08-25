"""The profile the app reads. Roadmap §5.

    GET /profile    free

Settings is where a user changes targets, cadence, platform preference and
resume retention, and every one of those paths was reading a mock.

## What is actually in `profiles`

Measured across all 12 live rows before writing the mapping:

    12/12   tone, posting_frequency, cv_template, plan, resume_retention_opt_in
    10/12   name, skills, target_roles, location, experience, education
     8/12   onboarded, cadence, last_seen_jobs_at
     4/12   target_role      (singular — `target_roles` is the plural one)
     2/12   niche, target_sectors
     1/12   personal_brand, linkedin_url, headshot_url, preferences
     0/12   platforms, key_achievement, content_avoid, linkedin_synced_at,
            linkedin_raw

Two rows have no name or skills at all — accounts that signed up and never
onboarded. Every field is therefore optional in practice, and the payload
passes absence through rather than filling it.

## `platforms` is null for every single user

`db_to_profile` defaults NULL to `["linkedin"]`, reasoning that existing V1
users are LinkedIn users by definition and should not be pushed back through
onboarding. That default is not an edge case — **it fires for 100% of the live
rows**, so it is the only thing standing between every user and an empty
platform list. Worth knowing that the Naukri track is opt-in from zero.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from carigma_api.auth.dependencies import CurrentUser
from carigma_api.config import Settings, get_settings
from carigma_api.services.repository import ProfileRepository, user_client

logger = logging.getLogger(__name__)

router = APIRouter(tags=["profile"])


def _profiles(request: Request, settings: Settings) -> ProfileRepository:
    token = (request.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
    return ProfileRepository(user_client(settings, token))


@router.get("/profile")
def get_profile(
    request: Request,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """This user's profile, in the repository's camelCase shape.

    An empty profile is a real state — two live accounts are in it — so it
    returns `{}` with `onboarded: false` rather than a 404. A failed READ is a
    503, because "we could not look" must not render as "you have not started".
    """
    try:
        profile = _profiles(request, settings).load(user.id)
    except Exception as exc:
        logger.exception("could not read the profile for %s", user.id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "read_failed", "message": "We couldn't load your profile."},
        ) from exc

    return {
        **profile,
        # Carried explicitly. Inferring "new user" from an empty dict would
        # make an unreadable profile and an unstarted one look identical.
        "onboarded": bool(profile.get("onboarded")),
    }


__all__ = ["router"]
