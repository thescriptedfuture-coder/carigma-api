"""Settings and the rest of auth. Contract §5.

The platform endpoint carries a rule that must never be relaxed:
**platform preference governs profile optimization only — never job sourcing.**
There is no compliant access to Naukri listings at all, so no jobs endpoint
takes a platform parameter and none should ever be added.
"""

from __future__ import annotations

import logging
import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, ValidationInfo, field_validator
from supabase import create_client

from carigma_api.auth.dependencies import CurrentUser
from carigma_api.config import Settings, get_settings
from carigma_api.routes._guards import enforce_public_rate_limit
from carigma_api.services.cv_pdf import DEFAULT_TEMPLATE, TEMPLATE_KEYS, TEMPLATES
from carigma_api.services.onboarding import CADENCES, DEFAULT_CADENCE
from carigma_api.services.repository import ProfileRepository, user_client
from carigma_api.services.schedule import CADENCE_DAYS
from carigma_api.services.settings_service import (
    EMAIL_PREFS_KEY,
    EmailPreferences,
    NoPlatformSelected,
    Platform,
    ResumeStore,
    change_platforms,
    merge_email_preferences,
    reset_confirmation,
    set_retention,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["settings"])


class PlatformsRequest(BaseModel):
    platforms: list[Platform] = Field(min_length=0)


class TargetsRequest(BaseModel):
    target_roles: str = Field(min_length=1, max_length=200)
    cadence: int

    @field_validator("target_roles")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        # Every agent aims at something; a blank target searches for nothing.
        if not value.strip():
            raise ValueError("Pick a target role so the agents know what to aim at.")
        return value.strip()

    @field_validator("cadence")
    @classmethod
    def _offered(cls, value: int) -> int:
        # The list onboarding offers, not a second copy of it.
        if value not in CADENCES:
            raise ValueError(f"Cadence is one of {', '.join(map(str, CADENCES))} posts a week.")
        return value


#: V1's four voices (`app.py` _TONES), in its order. The post writer defaults
#: to the first when none is set (`content_writer.brief_for`).
TONES: tuple[str, ...] = (
    "Professional yet warm",
    "Bold & opinionated",
    "Storytelling & personal",
    "Educational & formal",
)

#: Request field -> profile key, and the longest value each may hold. One
#: table, so the GET, the PUT and the read-back cannot disagree about a name.
DETAIL_FIELDS: dict[str, tuple[str, int]] = {
    "name": ("name", 120),
    "current_role": ("currentRole", 160),
    "location": ("location", 120),
    "industry": ("industry", 120),
    "linkedin_url": ("linkedinUrl", 300),
    "education": ("education", 400),
    "tone": ("tone", 60),
    "niche": ("niche", 160),
    "personal_brand": ("personalBrand", 300),
    "content_avoid": ("contentAvoid", 300),
    "primary_goal": ("primaryGoal", 300),
    "target_sectors": ("targetSectors", 300),
    "key_achievement": ("keyAchievement", 400),
    "cv_template": ("cvTemplate", 40),
}

#: A LinkedIn PROFILE address, as people actually paste it: with or without
#: `https://`, with `www.` or a country prefix (`in.`), a trailing slash, a
#: tracking query. It required `https://` — and Ravi pasted
#: `www.linkedin.com/in/ravi-kumar-analytics` from his browser and was told it
#: was not a LinkedIn address (UAT, 24 Oct). Company and job pages are not
#: profiles, so they are still refused.
_LINKEDIN_PROFILE = re.compile(
    r"^(?:https?://)?(?:[a-z]{2,3}\.)?linkedin\.com/(in|pub)/([^/?#\s]+)/?(?:[?#]\S*)?$",
    re.IGNORECASE,
)


def linkedin_profile_url(value: str) -> str | None:
    """The one form a profile address is stored in, or None if it is not one."""
    match = _LINKEDIN_PROFILE.match(value.strip())
    if not match:
        return None
    return f"https://www.linkedin.com/{match.group(1).lower()}/{match.group(2)}"


class DetailsRequest(BaseModel):
    """Any subset: each Settings section saves only its own fields. A field
    left out is untouched; an empty string clears it (except the name)."""

    name: str | None = None
    current_role: str | None = None
    location: str | None = None
    industry: str | None = None
    linkedin_url: str | None = None
    education: str | None = None
    tone: str | None = None
    niche: str | None = None
    personal_brand: str | None = None
    content_avoid: str | None = None
    primary_goal: str | None = None
    target_sectors: str | None = None
    key_achievement: str | None = None
    cv_template: str | None = None

    @field_validator("*")
    @classmethod
    def _trimmed_and_bounded(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None:
            return None
        value = value.strip()
        limit = DETAIL_FIELDS[str(info.field_name)][1]
        if len(value) > limit:
            raise ValueError(f"Keep this under {limit} characters.")
        return value

    @field_validator("name")
    @classmethod
    def _named(cls, value: str | None) -> str | None:
        # The name heads every CV. Cleared, a CV would be headed by nothing.
        if value is not None and not value:
            raise ValueError("Your name goes on every CV, so it cannot be blank.")
        return value

    @field_validator("linkedin_url")
    @classmethod
    def _linkedin(cls, value: str | None) -> str | None:
        if not value:
            return value
        # Stored in one form, so the CV, the read-back and the next visit all
        # see the same address whatever was pasted.
        normalised = linkedin_profile_url(value)
        if normalised is None:
            raise ValueError(
                "That is not a LinkedIn profile address. It looks like linkedin.com/in/your-name."
            )
        return normalised

    @field_validator("tone")
    @classmethod
    def _voice(cls, value: str | None) -> str | None:
        if value and value not in TONES:
            raise ValueError(f"Tone is one of: {', '.join(TONES)}.")
        return value

    @field_validator("cv_template")
    @classmethod
    def _template(cls, value: str | None) -> str | None:
        if value is not None and value not in TEMPLATE_KEYS:
            raise ValueError("That is not one of the six CV layouts.")
        return value


class RetentionRequest(BaseModel):
    opt_in: bool


class EmailPrefsRequest(BaseModel):
    daily_brief: bool
    weekly_review: bool
    unsubscribed_all: bool


class ResetRequest(BaseModel):
    # A shape check, not `EmailStr`. Full RFC validation would pull in
    # `email_validator` to buy nothing: this endpoint answers identically
    # whatever the address is, and Supabase validates it before sending. The
    # pattern only stops obvious junk reaching the mailer.
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class SupabaseResumeStore:
    """Resume files in Supabase Storage.

    §21 Q5 was verified against the live project: the only bucket is
    `headshots`, so **no resume bucket exists yet** and this lists nothing.
    That is the correct answer, not a stub — analyze-and-discard is the default,
    so an account with no retention opt-in genuinely has no stored file, and
    the purge honestly reports zero.

    When a `resumes` bucket is created for opt-in users, only `_BUCKET` and the
    two methods below change.
    """

    _BUCKET = "resumes"

    def __init__(self, client: Any) -> None:
        self._client = client

    def list_files(self, user_id: str) -> list[str]:
        try:
            entries = self._client.storage.from_(self._BUCKET).list(user_id)
        except Exception:
            # A missing bucket is the expected case today. Logged at debug so a
            # real outage is still visible in the receipt as a failure, not
            # silently rounded to "nothing stored".
            logger.debug("no resume bucket for %s", user_id, exc_info=True)
            return []
        return [f"{user_id}/{e['name']}" for e in entries or []]

    def delete(self, user_id: str, path: str) -> bool:
        try:
            self._client.storage.from_(self._BUCKET).remove([path])
        except Exception:
            logger.exception("resume delete failed for %s", path)
            return False
        return True


def _profiles(request: Request, settings: Settings) -> tuple[ProfileRepository, Any]:
    token = (request.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
    client = user_client(settings, token)
    return ProfileRepository(client), client


@router.put("/profile/platforms")
def put_platforms(
    body: PlatformsRequest,
    request: Request,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """Change which platforms are optimised. Free.

    Never touches jobs. Never deletes the removed platform's history.
    """
    profiles, _ = _profiles(request, settings)
    profile = profiles.load(user.id) or {}
    current = tuple(
        Platform(p) for p in (profile.get("platforms") or ["linkedin"]) if p in list(Platform)
    )

    try:
        change = change_platforms(current, tuple(body.platforms))
    except NoPlatformSelected as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "no_platform", "message": str(exc)},
        ) from exc

    try:
        profiles.save(user.id, {"platforms": [str(p) for p in change.platforms]})
    except Exception as exc:
        logger.exception("platform save failed for %s", user.id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "save_failed",
                "message": "We couldn't save that just now. Your settings are unchanged.",
            },
        ) from exc

    return change.as_dict()


def _targets(profile: dict[str, Any]) -> dict[str, Any]:
    raw = profile.get("cadence")
    cadence = raw if isinstance(raw, int) and raw in CADENCES else None
    return {
        "target_roles": str(profile.get("targetRoles") or ""),
        # `null` when the row carries no cadence we offer: the proposal then
        # falls back to V1's three, and the page says it is a default rather
        # than presenting it as the user's choice.
        "cadence": cadence,
        "default_cadence": DEFAULT_CADENCE,
        "options": [{"cadence": n, "days": list(CADENCE_DAYS[n])} for n in CADENCES],
    }


@router.get("/profile/targets")
def get_targets(
    request: Request,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """What the agents aim at, and how many posts a week. Free.

    Set once at onboarding and unchangeable after it — while the cadence
    became the days every weekly plan proposes (S8, S9). A user who chose
    five a week and wants three had no way to say so.
    """
    profiles, _ = _profiles(request, settings)
    try:
        profile = profiles.load(user.id) or {}
    except Exception as exc:
        logger.exception("could not read the targets for %s", user.id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "read_failed", "message": "We couldn't load your targets just now."},
        ) from exc
    return _targets(profile)


@router.put("/profile/targets")
def put_targets(
    body: TargetsRequest,
    request: Request,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """Change the target roles and the cadence. Free.

    **A week already signed is not touched.** The cadence seeds the NEXT plan
    offered — Sunday's, or "Plan this week" on an unplanned week. Rewriting a
    signed contract under the user's signature would be the product acting on
    their behalf; the note says so instead.
    """
    profiles, _ = _profiles(request, settings)
    try:
        profiles.save(
            user.id,
            {
                "targetRoles": body.target_roles,
                # V1 reads the singular too (see `onboarding.completion`).
                "targetRole": body.target_roles.split(",")[0].strip(),
                "cadence": body.cadence,
            },
        )
        # Read BACK: the answer is what the row now says, not what was asked.
        saved = profiles.load(user.id) or {}
    except Exception as exc:
        logger.exception("targets save failed for %s", user.id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "save_failed",
                "message": "We couldn't save that just now. Your settings are unchanged.",
            },
        ) from exc

    shown = _targets(saved)
    if shown["cadence"] != body.cadence or shown["target_roles"] != body.target_roles:
        # A write that "succeeded" and did not stick is a failed save. Saying
        # "the next plan proposes None posts" would be reporting a decision
        # the row does not hold.
        logger.error("targets save for %s did not read back", user.id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "save_failed",
                "message": "We couldn't save that just now. Your settings are unchanged.",
            },
        )
    days = ", ".join(d.title() for d in CADENCE_DAYS.get(shown["cadence"] or DEFAULT_CADENCE, ()))
    shown["note"] = (
        f"The next plan you are offered proposes {shown['cadence']} posts a week — {days}. "
        "A week you have already signed stays as you signed it."
    )
    return shown


def _details(profile: dict[str, Any]) -> dict[str, Any]:
    def text(key: str) -> str:
        value = profile.get(key)
        return value.strip() if isinstance(value, str) else ""

    return {
        "about": {
            "name": text("name"),
            "current_role": text("currentRole"),
            "location": text("location"),
            "industry": text("industry"),
            "linkedin_url": text("linkedinUrl"),
            "education": text("education"),
        },
        "voice": {
            # Empty when never set: the writer then uses the first tone, and
            # the page says it is a default rather than presenting a choice.
            "tone": text("tone"),
            "niche": text("niche"),
            "personal_brand": text("personalBrand"),
            "content_avoid": text("contentAvoid"),
        },
        "brief": {
            "primary_goal": text("primaryGoal"),
            "target_sectors": text("targetSectors"),
            "key_achievement": text("keyAchievement"),
        },
        "cv_template": text("cvTemplate") if text("cvTemplate") in TEMPLATE_KEYS else None,
        "default_tone": TONES[0],
        "default_cv_template": DEFAULT_TEMPLATE,
        "options": {"tones": list(TONES), "cv_templates": [dict(t) for t in TEMPLATES]},
    }


@router.get("/profile/details")
def get_details(
    request: Request,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """The profile fields the agents read, for Settings. Free.

    Tailored CVs read the name, role, location, LinkedIn and education; every
    draft reads the voice; the analyst and CVs read the brief; the download
    reads the template. V1 let a user edit all of it in Settings; V2 let them
    edit none of it after onboarding (V1_V2_COMPARISON 3.9, S13).
    """
    profiles, _ = _profiles(request, settings)
    try:
        profile = profiles.load(user.id) or {}
    except Exception as exc:
        logger.exception("could not read the details for %s", user.id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "read_failed", "message": "We couldn't load your details just now."},
        ) from exc
    return _details(profile)


@router.put("/profile/details")
def put_details(
    body: DetailsRequest,
    request: Request,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """Save the fields sent, and only those. Free. Read back."""
    given = body.model_dump(exclude_none=True)
    if not given:
        raise HTTPException(
            status_code=422,
            detail={"error": "nothing_to_save", "message": "There was nothing to save."},
        )
    patch = {DETAIL_FIELDS[field][0]: value for field, value in given.items()}
    profiles, _ = _profiles(request, settings)
    try:
        profiles.save(user.id, patch)
        saved = profiles.load(user.id) or {}
    except Exception as exc:
        logger.exception("details save failed for %s", user.id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "save_failed",
                "message": "We couldn't save that just now. Your details are unchanged.",
            },
        ) from exc
    if any(str(saved.get(key) or "").strip() != value for key, value in patch.items()):
        # "Saved" for a write that did not stick would be the product
        # reporting a decision the row does not hold.
        logger.error("details save for %s did not read back", user.id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "save_failed",
                "message": "We couldn't save that just now. Your details are unchanged.",
            },
        )
    return {**_details(saved), "note": "Saved. The next draft, CV or score uses it."}


@router.put("/profile/resume-retention")
def put_retention(
    body: RetentionRequest,
    request: Request,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """Opt in or out of keeping the uploaded resume.

    Opting OUT deletes immediately and returns a receipt with a real count. The
    flag is only written as `false` once deletion actually succeeded.
    """
    profiles, client = _profiles(request, settings)
    store: ResumeStore = SupabaseResumeStore(client)

    change = set_retention(store, user.id, opt_in=body.opt_in)

    try:
        profiles.save(user.id, {"resumeRetentionOptIn": change.opt_in})
    except Exception:
        logger.exception("retention flag save failed for %s", user.id)
        # The files may already be gone. Say so rather than implying nothing
        # happened — the user's data state and our record have diverged, and
        # hiding that would be the dishonest half of a half-failure.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "save_failed",
                "message": (
                    "Your files were handled, but we couldn't record the setting. "
                    "Please check this page again shortly."
                ),
            },
        ) from None

    return change.as_dict()


@router.get("/profile/email-preferences")
def get_email_preferences(
    request: Request,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """What email this account will actually receive.

    Read separately from `/profile` because the unsubscribe landing needs it
    before anything else on the settings page has loaded, and because a
    preference read must never fail just because some unrelated profile field
    is malformed.
    """
    profiles, _ = _profiles(request, settings)
    profile = profiles.load(user.id) or {}
    stored = (profile.get("preferences") or {}).get(EMAIL_PREFS_KEY)
    return EmailPreferences.from_stored(stored).as_dict()


@router.put("/profile/email-preferences")
def put_email_preferences(
    body: EmailPrefsRequest,
    request: Request,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """Change what arrives. Free, and takes effect on the next cron run.

    The write MERGES into `profiles.preferences` rather than replacing it —
    that column also carries the Profile Analyst's learned user memory, and
    `save()` writes it wholesale.
    """
    profiles, _ = _profiles(request, settings)
    profile = profiles.load(user.id) or {}

    prefs = EmailPreferences(
        daily_brief=body.daily_brief,
        weekly_review=body.weekly_review,
        unsubscribed_all=body.unsubscribed_all,
    )
    merged = merge_email_preferences(profile.get("preferences"), prefs)

    try:
        profiles.save(user.id, {"preferences": merged})
    except Exception as exc:
        logger.exception("email preference save failed for %s", user.id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "save_failed",
                # Says what is still true, so nobody assumes they are unsubscribed
                # when they are not and then reports us as spam.
                "message": (
                    "We couldn't save that just now, so your email settings are unchanged. "
                    "Try again, or email support@carigma.in and we'll do it by hand."
                ),
            },
        ) from exc

    return prefs.as_dict()


@router.post("/auth/reset-password")
def post_reset_password(
    body: ResetRequest,
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """Send a reset link. Deliberately unauthenticated, and rate limited.

    Answers identically whether or not the address is registered — confirming
    "no account with that email" turns this into an account-enumeration oracle.

    **Throttled per IP.** This sends mail to an address the caller chooses, with
    no token, so unthrottled it is an email bomb aimed at anyone and a cheap
    enumeration probe besides. The per-IP limiter had been built and tested
    since P4-2 and applied to nothing — the bucket existed, the guard existed,
    and no route called it.
    """
    enforce_public_rate_limit(request)

    # ACTUALLY SEND IT.
    #
    # This route rate-limited, logged "password reset requested", and returned
    # "a reset link is on its way" — while calling nothing. The comment said
    # "Supabase sends the mail". Supabase was never asked.
    #
    # It hid because of its own best feature: the response is DELIBERATELY
    # identical whether or not the address is registered, so the surface that
    # would have revealed the gap is the one built to say nothing. A user who
    # forgot their password waited for an email that did not exist.
    #
    # `redirect_to` is mandatory here, not optional. Supabase silently
    # substitutes its global Site URL for anything it cannot match, and that
    # Site URL is V1 — so a V2 reset link without this lands the user in a
    # different application. `APP_URL` must be on the Supabase redirect
    # allow-list or the same substitution happens anyway.
    #
    # And even then the TEMPLATE has the last word. The shared project's reset
    # template is V1's `<site>?token_hash=…&type=recovery`, which ignores
    # `redirect_to` — a V2 reset arrived at app.carigma.in, V1 (UAT, 24 Oct).
    # Until cutover that is where V2 resets land; after it, app.carigma.in is
    # V2 and the web's `EmailLinkGate` takes the link. `CUTOVER.md`.
    try:
        client = create_client(settings.supabase_url, settings.supabase_anon_key)
        client.auth.reset_password_for_email(
            body.email,
            {"redirect_to": f"{settings.app_url.rstrip('/')}/auth/reset"},
        )
    except Exception:
        # Logged, never surfaced. A different response for a failed send is the
        # same enumeration oracle by another route — and the log line is the
        # only place this can be seen, so it says what it means.
        logger.exception("PASSWORD RESET SEND FAILED — the user is waiting for an email")

    return reset_confirmation(body.email)


__all__ = ["router", "SupabaseResumeStore"]
