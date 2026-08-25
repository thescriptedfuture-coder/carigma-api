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
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from carigma_api.auth.dependencies import CurrentUser
from carigma_api.config import Settings, get_settings
from carigma_api.services.repository import user_client
from carigma_api.services.review import (
    Direction,
    ReviewFact,
    Streak,
    re_entry_contract,
    standing_by_card,
)
from carigma_api.services.weekly import (
    Cadence,
    ContractItem,
    ContractState,
    ContractTransitionError,
    approve,
    consecutive_lapses,
    decline,
    presentation_for,
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
    cadence: Cadence = Field(description="weekly | fortnightly")


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


def earned_review(
    *,
    facts: tuple[ReviewFact, ...],
    streak: Streak,
    milestone: dict[str, str] | None = None,
    built_at: datetime | None = None,
) -> dict[str, Any]:
    """What a closed week is worth keeping — the part that had to be earned.

    Facts without receipts cannot exist here; the type refuses them.

    **There is deliberately no `wire` key.** The review used to build its own
    market wire in parallel with the curated batch, which meant a second source
    of market claims that never passed through P6-2's publish gate: it could
    carry an item no human had checked. The path is deleted rather than left
    unrendered, because an orphaned field is a trap — the next person finds a
    plausible value and renders it.
    """
    return {
        "facts": [f.as_dict() for f in facts],
        "streak": streak.as_dict(),
        "milestone": milestone,
        # Stamped when the review is BUILT, and stored with it.
        #
        # The first version of this read the row's `created_at`, which is a
        # different event: the row is created when the week's CONTRACT is
        # proposed, on the Monday. The review is built the following Sunday.
        # Same row, six days apart — it would have reported every review as
        # built a week before it was.
        #
        # Before that it was the literal string "Sun 18:00", printed whatever
        # time the review actually ran.
        "built_at": (built_at or datetime.now(UTC)).isoformat(),
    }


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

    payload: dict[str, Any] = {
        "week_start": week_start.isoformat(),
        "lapsed_weeks": lapses,
        "presentation": presentation_for(lapses),
        "contract": contract.as_dict() if contract else None,
    }

    if contract is None:
        # Day one: a promise with a timestamp, never a blank surface.
        payload["day_one"] = {
            "happened": "Your first review has not been built yet.",
            "why": "Reviews are built Sunday evening from the week's real activity.",
            "next": "Your first review builds Sunday, 18:00.",
        }
    return payload


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
            "message": "See you Monday.",
            "detail": "The signed plan is now the Thread's queue.",
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

    Reviews are built from the week's REAL activity by the Sunday cron. Until
    that runs for a given week this returns null rather than a specimen: a
    plausible-looking review the user never earned is exactly the fabrication
    Bible §18.1 forbids.
    """
    store = _deps(request, settings)
    last_week = _monday_of(datetime.now(UTC).date()) - timedelta(weeks=1)

    try:
        earned = store.load_review(user.id, last_week)
    except Exception as exc:
        logger.exception("could not read the weekly review for %s", user.id)
        raise _unavailable("your review") from exc

    if earned is None:
        return {"review": None}
    return {"review": render_review(last_week, earned)}


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
    week_start = _monday_of(datetime.now(UTC).date())
    if store.load(user.id, week_start) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "week_exists",
                "message": "This week already has a plan.",
            },
        )

    proposed = re_entry_contract(
        week_start=week_start,
        candidates=_default_candidates(),
        cadence=cadence,
    )
    store.save(user.id, proposed)
    return {
        "contract": proposed.as_dict(),
        "note": "Re-entry weeks are smaller on purpose. The same signature, a lighter promise.",
    }


def _default_candidates() -> tuple[ContractItem, ...]:
    """The menu a re-entry week is trimmed from.

    Static for now: the generator that builds a personalised week is Content
    Intelligence's job and arrives with the Posts surface. These are real
    activities the product can actually deliver — no placeholder rows.
    """
    return (
        ContractItem("content", "THU", "One post slot", 10, 12),
        ContractItem("naukri", "SAT", "Freshness refresh only", 3, 10),
        ContractItem("review", "SUN", "Review, 18:00", 10, 0),
    )


__all__ = ["router", "earned_review", "render_review", "Direction", "ReviewFact"]
