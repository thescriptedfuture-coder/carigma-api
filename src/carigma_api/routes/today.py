"""Today and the thread. Roadmap §3.

    GET  /today                  free
    GET  /thread                 free
    POST /thread/{key}/decide    free — dismiss or snooze

The primary screen of the product, and until now it had no server at all: the
web called `/today` and `/thread` and MSW answered. Found by asking which
endpoints the app calls that the API does not serve.

Everything here is free. Reading your own state is not a run.

## Derived candidates, persisted decisions

The thread is computed from the same signals as the ladder, so there is one
source of truth for "what is going on" rather than two that drift. What is
stored is only what cannot be derived: that the user dismissed or snoozed
something.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from carigma_api.auth.dependencies import CurrentUser
from carigma_api.config import Settings, get_settings
from carigma_api.services.ladder import choose
from carigma_api.services.repository import ProfileRepository, user_client
from carigma_api.services.thread_store import (
    Decision,
    SupabaseThreadStore,
    surviving,
)
from carigma_api.services.today import load_signals

logger = logging.getLogger(__name__)

router = APIRouter(tags=["today"])

#: How long "later" lasts. Long enough to be a real reprieve, short enough that
#: a snooze is not a dismissal in disguise.
SNOOZE_FOR = timedelta(days=3)


class DecideRequest(BaseModel):
    decision: str = Field(pattern="^(dismissed|snoozed)$")


def _deps(request: Request, settings: Settings) -> tuple[Any, ProfileRepository, Any]:
    token = (request.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
    client = user_client(settings, token)
    return client, ProfileRepository(client), SupabaseThreadStore(client)


def _unavailable(what: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={"error": "read_failed", "message": f"We couldn't load {what} just now."},
    )


def _greeting(now: datetime) -> str:
    hour = now.hour
    if hour < 12:
        return "Morning"
    return "Afternoon" if hour < 17 else "Evening"


@router.get("/today")
def get_today(
    request: Request,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """One primary action, and everything it displaced.

    The profile read is the only hard dependency: without it there is no
    `lastSeenJobsAt`, and guessing would produce exactly the "everything is
    new" claim this path exists to avoid. Every other source degrades to
    silence rather than to a fabricated signal.
    """
    client, profiles, thread = _deps(request, settings)

    try:
        profile = profiles.load(user.id)
    except Exception as exc:
        logger.exception("could not read the profile for %s", user.id)
        raise _unavailable("your day") from exc

    now = datetime.now(UTC)
    signals = load_signals(client, user.id, profile, now=now)
    decision = choose(signals)

    try:
        decided = thread.decisions(user.id)
    except Exception:
        # A failed read of the decisions means we do not know what was
        # dismissed. Showing everything is the recoverable direction — the
        # alternative silently hides items the user never decided about.
        logger.exception("could not read thread decisions for %s", user.id)
        decided = {}

    return {
        "greeting": f"{_greeting(now)}{', ' + profile['name'].split()[0] if profile.get('name') else ''}",
        **decision.as_dict(),
        # The deferred rungs, minus anything the user has already answered.
        "deferred": surviving(decision.deferred, decided, now=now),
        "onboarded": bool(profile.get("onboarded")),
    }


@router.get("/thread")
def get_thread(
    request: Request,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """Everything worth surfacing, in ladder order, minus what was decided.

    The same computation as `/today` — the thread is not a second opinion, it
    is the whole list where Today shows the head of it.
    """
    client, profiles, thread = _deps(request, settings)

    try:
        profile = profiles.load(user.id)
    except Exception as exc:
        logger.exception("could not read the profile for %s", user.id)
        raise _unavailable("your thread") from exc

    now = datetime.now(UTC)
    decision = choose(load_signals(client, user.id, profile, now=now))

    try:
        decided = thread.decisions(user.id)
    except Exception:
        logger.exception("could not read thread decisions for %s", user.id)
        decided = {}

    everything = [decision.primary, *decision.deferred]
    return {"items": surviving(everything, decided, now=now)}


@router.post("/thread/{item_key}/decide")
def decide_thread_item(
    item_key: str,
    body: DecideRequest,
    request: Request,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """Record a dismissal or a snooze. Free — it is the user teaching us.

    The response is read BACK from the store rather than echoed from the
    request. A snooze that did not persist is a rung that reappears on the next
    load, and only the row can tell the difference between "recorded" and
    "accepted and dropped".
    """
    _client, _profiles, thread = _deps(request, settings)
    choice = Decision(body.decision)
    until = datetime.now(UTC) + SNOOZE_FOR if choice is Decision.SNOOZED else None

    try:
        thread.record(user.id, item_key, choice, snooze_until=until)
        stored = thread.decisions(user.id).get(item_key)
    except Exception as exc:
        logger.exception("could not record a thread decision for %s", user.id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "save_failed",
                "message": "We couldn't save that just now. Nothing has changed.",
            },
        ) from exc

    if stored is None:
        # The write reported success and the row is not there. Saying so beats
        # confirming a decision that will not survive the next load.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "save_failed",
                "message": "We couldn't save that just now. Nothing has changed.",
            },
        )

    return {
        "item_key": item_key,
        "decision": stored.get("decision"),
        "snooze_until": stored.get("snooze_until"),
    }


__all__ = ["SNOOZE_FOR", "router"]
