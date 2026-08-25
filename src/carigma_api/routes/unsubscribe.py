"""Public unsubscribe. Contract §22.

    GET  /unsubscribe/{token}   free, PUBLIC — can we read this link?
    POST /unsubscribe/{token}   free, PUBLIC — turn marketing email off

**Outside the auth guard, deliberately.** An unsubscribe link arrives by email,
usually on a device where the person was never signed in. Requiring a login
would make "unsubscribe" mean "sign in first", which is a bad answer and for
marketing mail arguably not a lawful one.

The token identifies the SUBSCRIPTION, not a session — see
`services/unsubscribe`.

## What this can and cannot do

It can set `unsubscribed_all`. That is the entire surface. It cannot read a
profile, cannot say who the token belongs to, and cannot change anything else,
because nothing here accepts a scope or an action.

Service-role, because there is no session to carry — and that is exactly why
the route is this narrow. A service-role handler with a general-purpose
parameter would be one missing check away from serving anyone's data.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from carigma_api.config import Settings, get_settings
from carigma_api.routes._guards import enforce_public_rate_limit
from carigma_api.services import unsubscribe as unsub
from carigma_api.services.repository import ProfileRepository, service_client
from carigma_api.services.settings_service import (
    EMAIL_PREFS_KEY,
    EmailPreferences,
    merge_email_preferences,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["unsubscribe"])


@router.get("/unsubscribe/{token}")
def describe(
    token: str,
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """Whether the LINK is readable. Never who it belongs to.

    A valid token gets `readable: true` and no identity — the page it renders
    already knows what it is for, and naming an address would confirm that
    address is registered to whoever is holding the link.
    """
    enforce_public_rate_limit(request)
    readable = unsub.resolve(token, service_key=settings.supabase_service_key) is not None

    return {
        "readable": readable,
        "scope": "marketing emails",
        # The 'unknown' state, kept. A link an email client truncated is the
        # common case, and it is not the reader's fault.
        "note": (
            "One click and marketing email stops. Receipts and anything you ask "
            "us for directly are unaffected."
            if readable
            else "We can't read that link — it may have been cut short by your email app."
        ),
    }


@router.post("/unsubscribe/{token}")
def apply(
    token: str,
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """Turn marketing email off. One click, no login, idempotent.

    **Always returns the same body.** A token we honoured and one we could not
    read are indistinguishable to the caller, which is the non-enumeration rule
    password reset already follows. Someone probing with guessed tokens learns
    nothing.

    A write failure is also not surfaced — but it IS logged loudly, because
    "we said done and did nothing" is the exact shape this project keeps
    finding, and here it would leave someone receiving mail they refused.
    """
    enforce_public_rate_limit(request)
    user_id = unsub.resolve(token, service_key=settings.supabase_service_key)

    if user_id is None:
        logger.info("unsubscribe attempted with an unreadable token")
        return unsub.DONE

    try:
        client = service_client(settings)
        profiles = ProfileRepository(client)
        stored = profiles.load(user_id) or {}

        # MERGE into the whole `preferences` blob, never a bare column write.
        #
        # The first draft did `update({"email_unsubscribed_all": True})` — a
        # column that does not exist. It bypassed `profile_to_db`, which raises
        # on unknown keys, by using the raw client; Postgres would have
        # rejected it, this `except` would have swallowed it, and the person
        # would have kept receiving mail they refused. Exactly the shape this
        # project keeps finding, one line from shipping.
        #
        # `preferences` also carries the Profile Analyst's learned memory, and
        # `save()` writes the column wholesale — hence the merge helper rather
        # than a fresh dict.
        # `from_stored` takes the EMAIL SUB-DICT, not the whole blob — the
        # same extraction `routes/settings.py` does. Passing the blob makes
        # every `.get` fall to its default, which reads as "opted in to
        # everything" and would have silently re-enabled the two per-type
        # flags on every unsubscribe. Caught by the round-trip test.
        current = EmailPreferences.from_stored(
            (stored.get("preferences") or {}).get(EMAIL_PREFS_KEY)
        )
        merged = merge_email_preferences(
            stored.get("preferences"),
            EmailPreferences(
                # Their per-type choices are preserved. Unsubscribing is a
                # master switch, not an erasure of what they picked — if they
                # ever resubscribe, they get their settings back rather than
                # our defaults.
                daily_brief=current.daily_brief,
                weekly_review=current.weekly_review,
                unsubscribed_all=True,
            ),
        )
        profiles.save(user_id, {"preferences": merged})
    except Exception:
        # Loud, because a silent failure here means we keep emailing somebody
        # who told us to stop.
        logger.exception("UNSUBSCRIBE WRITE FAILED for %s — they will keep receiving mail", user_id)

    return unsub.DONE


__all__ = ["router"]
