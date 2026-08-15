"""Weekly contract endpoints — contract §15.

Thin by design. Every rule that matters lives in `services.weekly` and
`services.review`, so the state machine is testable without HTTP and the
`approved` / `auto_adopted` distinction has exactly one implementation.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from carigma_api.auth.dependencies import CurrentUser
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
    WeeklyContract,
    approve,
    consecutive_lapses,
    decline,
    presentation_for,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/weekly", tags=["weekly"])


class ApproveRequest(BaseModel):
    week_start: date
    # Omitted means "all of it". An explicit empty list is a decline, and is
    # recorded as one — see services.weekly.approve.
    accept: list[str] | None = None


class CadenceRequest(BaseModel):
    cadence: Cadence = Field(description="weekly | fortnightly")


# ── Storage seam ───────────────────────────────────────────────────────────
# P4 ships the state machine and the surface. Persistence lands with the
# agent_runs work (task P4-2), which replaces the in-memory stores wholesale;
# splitting that migration across two tasks would leave two half-migrations.
# The seam is explicit so the swap touches this block and nothing else.

_CONTRACTS: dict[str, dict[str, WeeklyContract]] = {}
# Built reviews, keyed by the week they cover. Populated by the Sunday cron.
_REVIEWS: dict[str, dict[str, dict[str, Any]]] = {}


def _key(week_start: date) -> str:
    return week_start.isoformat()


def _history(user_id: str) -> list[WeeklyContract]:
    return sorted(_CONTRACTS.get(user_id, {}).values(), key=lambda c: c.week_start)


def _store(user_id: str, contract: WeeklyContract) -> None:
    _CONTRACTS.setdefault(user_id, {})[_key(contract.week_start)] = contract


def _load(user_id: str, week_start: date) -> WeeklyContract | None:
    return _CONTRACTS.get(user_id, {}).get(_key(week_start))


def reset_store() -> None:
    """Test seam. Not exported through the API."""
    _CONTRACTS.clear()
    _REVIEWS.clear()


def seed(user_id: str, contract: WeeklyContract) -> None:
    """Test/dev seam for arranging a user's history."""
    _store(user_id, contract)


def seed_review(user_id: str, week_start: date, review: dict[str, Any]) -> None:
    """Test/dev seam for a built review."""
    _REVIEWS.setdefault(user_id, {})[_key(week_start)] = review


def build_review(
    *,
    week_start: date,
    facts: tuple[ReviewFact, ...],
    streak: Streak,
    milestone: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Assemble a review payload from real activity.

    Facts without receipts cannot exist here — the type refuses them.

    **There is deliberately no `wire` key.** The review used to build its own
    market wire in parallel with the curated batch, which meant a second source
    of market claims that never passed through P6-2's publish gate: it could
    carry an item no human had checked. The path is deleted rather than left
    unrendered, because an orphaned field is a trap — the next person finds a
    plausible value and renders it.

    The market read now comes from `services.market.wire_for` alone, through
    `GET /market/wire`, for every surface and for the email. One curation, no
    second implementation to drift.
    """
    week_end = week_start + timedelta(days=6)
    return {
        "week_label": f"Week of {week_start.day:02d}-{week_end.day:02d} {week_end:%b}",
        "built_at": "Sun 18:00",
        "reading_minutes": max(1, round(len(facts) * 0.8)),
        "streak": streak.as_dict(),
        "facts": [f.as_dict() for f in facts],
        "milestone": milestone,
    }


# ── Endpoints ──────────────────────────────────────────────────────────────


def _monday_of(value: date) -> date:
    return value - timedelta(days=value.weekday())


@router.get("/contract")
def get_contract(user: CurrentUser) -> dict[str, Any]:
    """The current week's contract, plus how the lapse (if any) is presented."""
    today = datetime.now(UTC).date()
    week_start = _monday_of(today)
    contract = _load(user.id, week_start)

    history = _history(user.id)
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
def approve_contract(body: ApproveRequest, user: CurrentUser) -> dict[str, Any]:
    """The signature. The only path to `state = approved`."""
    contract = _load(user.id, body.week_start)
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

    _store(user.id, signed)
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
def decline_contract(body: ApproveRequest, user: CurrentUser) -> dict[str, Any]:
    contract = _load(user.id, body.week_start)
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

    _store(user.id, declined)
    return {"contract": declined.as_dict()}


@router.get("/review")
def get_review(user: CurrentUser) -> dict[str, Any]:
    """The Sunday review — the record, before the contract.

    Returns `{"review": null}` when no review has been built yet, rather than
    404. A missing review is not an error, and the client renders the day-one
    promise for it — "your first review builds Sunday, 18:00" — which is a
    different thing from "we tried to build one and failed".

    Reviews are built from the week's REAL activity by the Sunday cron, which
    lands with the Posts surface (the content loop is most of what a review
    reports on). Until then this returns null rather than a specimen: a
    plausible-looking review the user never earned is exactly the fabrication
    Bible §18.1 forbids.
    """
    today = datetime.now(UTC).date()
    stored = _REVIEWS.get(user.id, {}).get(_key(_monday_of(today) - timedelta(weeks=1)))
    return {"review": stored}


@router.get("/standing-by")
def get_standing_by(user: CurrentUser) -> dict[str, Any]:
    """The lapsed presentation — facts about the absence, then two doors."""
    today = datetime.now(UTC).date()
    history = _history(user.id)
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
def propose_re_entry(user: CurrentUser, body: CadenceRequest | None = None) -> dict[str, Any]:
    """ "Pick up the thread" — a lighter week, still needing a signature."""
    cadence = body.cadence if body else Cadence.WEEKLY
    week_start = _monday_of(datetime.now(UTC).date())
    if _load(user.id, week_start) is not None:
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
    _store(user.id, proposed)
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


__all__ = ["router", "reset_store", "seed", "Direction", "ReviewFact"]
