"""Weekly contract endpoints — contract §15.

Thin by design. Every rule that matters lives in `services.weekly` and
`services.review`, so the state machine is testable without HTTP and the
`approved` / `auto_adopted` distinction has exactly one implementation.

## Ported off the module dicts

`_CONTRACTS` and `_REVIEWS` were plain dicts behind a comment promising
persistence "lands with P4-2". It did not. Every approved contract was lost on
restart, and the Sunday sweep — a question about all users, asked from a
process that serves none of them — could only ever find an empty dict and
truthfully report that nothing needed adopting.

Both now go through `SupabaseContractStore` over `weekly_review`.

## What the port forced a decision about

**Where a review lives.** On the same row as the contract for the week it
covers, in `facts`. A review of week N and the plan for week N+1 appear on the
same Sunday screen but are different weeks, so they are different rows.

**What is stored, and what is derived.** Only what the week EARNED — the facts,
the streak, any milestone. The week label and the reading estimate are copy,
derived at read time; freezing them in the database means a wording change
needs a data migration and old rows keep saying the old thing forever.

**`built_at` was a lie.** It was the literal string `"Sun 18:00"`, printed
whatever time the review was actually assembled. It now comes from the row.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from carigma_api.auth.dependencies import CurrentUser
from carigma_api.config import Settings, get_settings
from carigma_api.services.repository import ProfileRepository, user_client
from carigma_api.services.review import (
    Direction,
    ReviewFact,
    Streak,
    earned_review,
    re_entry_contract,
    standing_by_card,
)
from carigma_api.services.schedule import proposal_items
from carigma_api.services.weekly import (
    Cadence,
    ContractState,
    ContractTransitionError,
    WeeklyContract,
    approve,
    consecutive_lapses,
    decline,
    presentation_for,
    release,
)
from carigma_api.services.weekly_store import ContractStore, SupabaseContractStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/weekly", tags=["weekly"])


class ApproveRequest(BaseModel):
    week_start: date
    # Omitted means "all of it". An explicit empty list is a decline, and is
    # recorded as one — see services.weekly.approve.
    accept: list[str] | None = None


class CadenceRequest(BaseModel):
    #: An OPTIONAL BODY IS NOT AN OPTIONAL FIELD. `body: CadenceRequest | None =
    #: None` accepts no body at all, and the moment a body IS present every
    #: field in it is required again — so `{}`, which is what the web sends from
    #: a button with nothing to choose, was answered with 422 "cadence: Field
    #: required" for as long as the endpoint has existed. `None` and `{}` are
    #: different bodies.
    #:
    #: `= Field(...)` with no `default=` reads as a default and is not one. That
    #: is the trap, and it is why the fix is a default here rather than special
    #: handling at the call site.
    cadence: Cadence = Field(default=Cadence.WEEKLY, description="weekly | fortnightly")


# ── Storage ────────────────────────────────────────────────────────────────


def _deps(request: Request, settings: Settings) -> ContractStore:
    """A store bound to the CALLER's token, so RLS applies to every read."""
    token = (request.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
    return SupabaseContractStore(user_client(settings, token))


def _unavailable(what: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={"error": "read_failed", "message": f"We couldn't load {what} just now."},
    )


def render_review(week_start: date, earned: dict[str, Any]) -> dict[str, Any]:
    """Dress the stored week in the copy that describes it.

    Split from `earned_review` so the presentation can change without a data
    migration. `reading_minutes` is a function of how many facts there are, so
    it is recomputed rather than stored — a stored estimate would keep quoting
    a length the payload no longer has.
    """
    facts = earned.get("facts")
    facts = facts if isinstance(facts, list) else []
    week_end = week_start + timedelta(days=6)
    return {
        "week_label": f"Week of {week_start.day:02d}-{week_end.day:02d} {week_end:%b}",
        "built_at": earned.get("built_at"),
        "reading_minutes": max(1, round(len(facts) * 0.8)),
        "streak": earned.get("streak"),
        "facts": facts,
        "milestone": earned.get("milestone"),
    }


# ── Endpoints ──────────────────────────────────────────────────────────────


#: Weekday index per contract day code, matching `date.weekday()`.
_DAY_INDEX = {"MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6}


def _monday_of(value: date) -> date:
    return value - timedelta(days=value.weekday())


@router.get("/contract")
def get_contract(
    request: Request,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """The current week's contract, plus how the lapse (if any) is presented."""
    store = _deps(request, settings)
    today = datetime.now(UTC).date()
    week_start = _monday_of(today)

    try:
        history = store.history(user.id)
    except Exception as exc:
        # A failed read is NOT "no history". Rendering the day-one promise here
        # would tell a user with fifteen weeks behind them that they have never
        # had a plan, and `consecutive_lapses` over an empty list would report
        # no lapse on a user who has lapsed for a month.
        logger.exception("could not read the weekly history for %s", user.id)
        raise _unavailable("your week") from exc

    contract = next((c for c in history if c.week_start == week_start), None)
    lapses = consecutive_lapses(history, upto=week_start)
    # Sunday evening's proposal is for the week that starts tomorrow. Without
    # this, the plan the review email asks someone to sign was invisible until
    # Monday — the one day it is no longer next week's.
    upcoming = next((c for c in history if c.week_start == week_start + timedelta(weeks=1)), None)

    payload: dict[str, Any] = {
        "week_start": week_start.isoformat(),
        "lapsed_weeks": lapses,
        "presentation": presentation_for(lapses),
        "contract": contract.as_dict() if contract else None,
        "next_week": upcoming.as_dict() if upcoming else None,
    }

    if not history:
        # Day one: a promise with a timestamp, never a blank surface.
        #
        # Keyed on HISTORY, not on this week's row. It was `contract is None`,
        # so a user four weeks in who had not planned this week yet was told
        # their FIRST review had not been built. And the promise is now the
        # true one: Sunday's pass reviews the weeks people plan, so a review
        # follows a plan — "your first review builds Sunday" was unconditional.
        payload["day_one"] = {
            "happened": "Your first review has not been built yet.",
            "why": "Reviews are built Sunday evening from the weeks you plan.",
            "next": "Plan this week, and its review builds Sunday at 18:00.",
        }
    if contract is not None and contract.state.is_decided:
        # The same copy the signature returned, on a reload. It used to exist
        # only in the POST response, so the page had to carry its own literal —
        # and that literal said "See you Monday" on a Thursday, forever.
        payload["confirmation"] = signing_copy(contract, today=today)
    return payload


def signing_copy(contract: WeeklyContract, *, today: date) -> dict[str, str]:
    """What a signature means, on the day it is given.

    **"See you Monday." was a literal**, on the server and again in the web
    component beside it, and it was the only thing either of them said. Signing
    a week that has already started — which is what re-entry proposes, and what
    anyone arriving on a Thursday signs — was answered with an instruction to
    come back in three days.

    A user can sign up on any day. Nothing in this cycle may assume Monday is
    near, so the copy is derived from the week being signed rather than
    asserted.
    """
    if contract.week_start > today:
        return {
            "message": "See you Monday.",
            "detail": "The signed plan is now the Thread's queue.",
        }
    remaining = [i for i in contract.in_force_items if _DAY_INDEX.get(i.day, 6) >= today.weekday()]
    if not remaining:
        # Signed after everything in it has passed. Saying "see you Monday"
        # here would at least be true, but the week is the one that just ran.
        return {
            "message": "Signed, with the week already behind it.",
            "detail": "Nothing is left on this one. Next week's plan arrives Sunday.",
        }
    first = remaining[0]
    return {
        "message": f"This week is yours. Next up: {first.day.title()}.",
        "detail": f"{first.summary} — {len(remaining)} left on the plan.",
    }


@router.post("/contract/approve")
def approve_contract(
    body: ApproveRequest,
    request: Request,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """The signature. The only path to `state = approved`."""
    store = _deps(request, settings)
    contract = store.load(user.id, body.week_start)
    if contract is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "no_contract", "message": "There is no plan for that week."},
        )

    try:
        signed = approve(
            contract,
            accept=tuple(body.accept) if body.accept is not None else None,
            now=datetime.now(UTC),
        )
    except ContractTransitionError as exc:
        # The user sees a plain sentence; the reason is logged, not leaked.
        logger.info("contract transition refused: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "already_decided",
                "message": "That week has already been settled.",
            },
        ) from exc

    store.save(user.id, signed)
    return {
        "contract": signed.as_dict(),
        # Brief 3a: "APPROVED · SUN 09 AUG · 18:12 / See you Monday."
        "confirmation": {
            "state": str(signed.state),
            "decided_at": signed.decided_at.isoformat() if signed.decided_at else None,
            **signing_copy(signed, today=datetime.now(UTC).date()),
        },
    }


@router.post("/contract/decline")
def decline_contract(
    body: ApproveRequest,
    request: Request,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    store = _deps(request, settings)
    contract = store.load(user.id, body.week_start)
    if contract is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "no_contract", "message": "There is no plan for that week."},
        )
    try:
        declined = decline(contract, now=datetime.now(UTC))
    except ContractTransitionError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "already_decided",
                "message": "That week has already been settled.",
            },
        ) from exc

    store.save(user.id, declined)
    return {"contract": declined.as_dict()}


@router.post("/contract/release")
def release_contract(
    body: ApproveRequest,
    request: Request,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """Withdraw a signature so the week can be edited and signed again.

    The exit an approved week did not have. Free: changing your mind about
    unstarted work is not a run.
    """
    store = _deps(request, settings)
    contract = store.load(user.id, body.week_start)
    if contract is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "no_contract", "message": "There is no plan for that week."},
        )
    try:
        reopened = release(contract, now=datetime.now(UTC))
    except ContractTransitionError as exc:
        logger.info("contract release refused: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "not_signed",
                "message": "That week has no signature to withdraw.",
            },
        ) from exc

    store.save(user.id, reopened)
    return {
        "contract": reopened.as_dict(),
        "note": "Your signature is withdrawn. Nothing runs until you sign again.",
    }


@router.get("/review")
def get_review(
    request: Request,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """The Sunday review — the record, before the contract.

    Covers the week that just CLOSED, so it reads last Monday's row. This
    week's row holds the plan being proposed; they appear on the same screen
    and are different weeks.

    Three answers, and they are three different things:

    - `{"review": null}` — no review has been built. Not an error and not a
      404; the client renders the day-one promise. A user in their first week
      is here.
    - `{"review": {...facts: []}}` — a review WAS built and the week produced
      nothing worth reporting. A real answer about a real quiet week, and it
      must not be redrawn as "your first review builds Sunday".
    - 503 — we could not read. "We couldn't look" is not "there is nothing",
      and rendering the day-one promise on a failed read would tell a
      long-standing user they have never had a review.

    Reviews are built from the week's REAL activity by the Sunday cron
    (`services.sunday`). Until that runs for a given week this returns null
    rather than a specimen: a plausible-looking review the user never earned is
    exactly the fabrication Bible §18.1 forbids.

    **On a Sunday, this week's review comes first.** The cron builds it at
    18:00 IST, the evening the week closes, and reading only "last Monday's
    row" would hide it until Monday — the review email arriving for a review
    the page could not show. Before the cron runs, Sunday still shows last
    week's.
    """
    store = _deps(request, settings)
    today = datetime.now(UTC).date()
    last_week = _monday_of(today) - timedelta(weeks=1)
    weeks = (_monday_of(today), last_week) if today.weekday() == 6 else (last_week,)

    try:
        for week in weeks:
            earned = store.load_review(user.id, week)
            if earned is not None:
                return {"review": render_review(week, earned)}
    except Exception as exc:
        logger.exception("could not read the weekly review for %s", user.id)
        raise _unavailable("your review") from exc
    return {"review": None}


@router.get("/standing-by")
def get_standing_by(
    request: Request,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """The lapsed presentation — facts about the absence, then two doors."""
    store = _deps(request, settings)
    today = datetime.now(UTC).date()
    try:
        history = store.history(user.id)
    except Exception as exc:
        logger.exception("could not read the weekly history for %s", user.id)
        raise _unavailable("your week") from exc
    lapses = consecutive_lapses(history, upto=_monday_of(today))

    if lapses == 0:
        return {"presentation": "none", "lapsed_weeks": 0}

    lapsed_weeks = [c for c in history if c.state is ContractState.AUTO_ADOPTED]
    since = lapsed_weeks[0].week_start if lapsed_weeks else _monday_of(today)

    # In P4 the facts come from the surfaces that produced them. Until the
    # aggregation lands, an absent source yields NO fact rather than a
    # placeholder — the ProviderUnavailable distinction: "nothing to report"
    # and "we couldn't look" must never render as the same thing.
    facts: tuple[ReviewFact, ...] = ()

    return standing_by_card(
        since=since,
        lapses=lapses,
        facts=facts,
        streak=Streak(weeks=0),
    )


@router.post("/contract/re-entry")
def propose_re_entry(
    request: Request,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
    body: CadenceRequest | None = None,
) -> dict[str, Any]:
    """ "Pick up the thread" — a lighter week, still needing a signature."""
    store = _deps(request, settings)
    cadence = body.cadence if body else Cadence.WEEKLY
    today = datetime.now(UTC).date()
    week_start = _monday_of(today)
    _refuse_if_planned(store, user.id, week_start)

    proposed = replace(
        re_entry_contract(
            week_start=week_start,
            candidates=proposal_items(_profile(request, settings, user.id), today=today),
            cadence=cadence,
        ),
        proposed_at=datetime.now(UTC),
    )
    store.save(user.id, proposed)
    return {
        "contract": proposed.as_dict(),
        "note": "Re-entry weeks are smaller on purpose. The same signature, a lighter promise.",
    }


@router.post("/contract/plan")
def propose_plan(
    request: Request,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """Propose this week from the user's own cadence. Still needs a signature.

    S8: "cadence becomes the default proposal; the signed week is the
    schedule." "Plan this week" used to call re-entry — the LIGHTER week for
    someone coming back after a gap — so planning on purpose always proposed
    one post on Thursday, whatever cadence the user chose at onboarding. This
    proposes their days. Re-entry stays the smaller promise it was built to be.
    """
    store = _deps(request, settings)
    today = datetime.now(UTC).date()
    week_start = _monday_of(today)
    _refuse_if_planned(store, user.id, week_start)

    profile = _profile(request, settings, user.id)
    proposed = WeeklyContract(
        week_start=week_start,
        items=proposal_items(profile, today=today),
        cadence=Cadence.WEEKLY,
        # When it was proposed is a fact, and nothing recorded it.
        proposed_at=datetime.now(UTC),
    )
    store.save(user.id, proposed)
    posts = sum(1 for i in proposed.items if i.kind == "content")
    return {
        "contract": proposed.as_dict(),
        "note": (
            f"{posts} post{'s' if posts != 1 else ''} on the days you post, for what is left "
            "of this week. Nothing is scheduled until you sign it."
            if posts
            else "No posting days are left this week. Nothing is scheduled until you sign it."
        ),
    }


def _refuse_if_planned(store: ContractStore, user_id: str, week_start: date) -> None:
    if store.load(user_id, week_start) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "week_exists",
                "message": "This week already has a plan.",
            },
        )


def _profile(request: Request, settings: Settings, user_id: str) -> dict[str, Any]:
    """The profile a proposal is seeded from. Unreadable is refused, not
    guessed: a proposal built on a default cadence would offer days the user
    never chose, under their name."""
    token = (request.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
    try:
        return ProfileRepository(user_client(settings, token)).load(user_id)
    except Exception as exc:
        logger.exception("could not read the profile for %s", user_id)
        raise _unavailable("your profile") from exc


__all__ = ["router", "earned_review", "render_review", "Direction", "ReviewFact"]
