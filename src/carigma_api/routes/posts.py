"""Posts endpoints — the content practice. Contract §8.

The endpoint names carry the honesty rule. There is no `POST /posts/publish`,
because this API publishes nothing anywhere. `mark-published` is a user
assertion about the real world, and the streak counts those and nothing else.

Free vs charged, and why:

- **`mark-published` is free.** Charging someone to tell us they did the work
  would be charging for our own record-keeping.
- **`skip` is free** — declining teaches the agent, so pricing it would
  suppress the exact signal Content Intelligence learns voice from (§21 Q4).
- **`regenerate` costs 3** (`regenerate_slot`), and goes through
  `services.runs.execute`, which is the only path from work to a charge.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from carigma_api.auth.dependencies import CurrentUser
from carigma_api.config import Settings, get_settings
from carigma_api.routes._guards import enforce_agent_rate_limit, idempotency_key, replay_if_seen
from carigma_api.services import credits as credits_service
from carigma_api.services import runs as runs_service
from carigma_api.services.ai import UpstreamError
from carigma_api.services.credits import InsufficientCredits
from carigma_api.services.posts import (
    Draft,
    ReviewNote,
    SkipReason,
    Slot,
    SlotState,
    SlotTransitionError,
    WeekPlan,
    default_week,
    mark_published,
    skip,
)
from carigma_api.services.posts_store import SupabasePlanStore
from carigma_api.services.repository import SupabaseCreditStore, user_client
from carigma_api.services.run_store import SupabaseRunStore
from carigma_api.services.runs import RunReporter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/posts", tags=["posts"])


class SkipRequest(BaseModel):
    reason: SkipReason | None = None
    note: str | None = Field(default=None, max_length=280)


class MarkPublishedRequest(BaseModel):
    #: What the user says they did. Optional — if they don't say, we stamp now,
    #: and either way it is THEIR claim, not our observation.
    at: str | None = None


# ── Storage ────────────────────────────────────────────────────────────────
#
# This was `_PLANS: dict[str, dict[str, WeekPlan]]` — a module dict, with a
# comment promising persistence would land in P4-2. It did not, so every draft,
# publish mark and skip reason lived until the next restart.
#
# The rule the schema enforces: **a single-slot edit writes a single row.**
# `save_week` is for planning a week; everything a user does to one day goes
# through `save_slot`, so two tabs editing different days cannot erase each
# other.


def _store(request: Request, settings: Settings) -> SupabasePlanStore:
    token = (request.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
    return SupabasePlanStore(user_client(settings, token))


def _load_or_default(store: SupabasePlanStore, user_id: str, week_start: date) -> WeekPlan:
    """The stored week, or a fresh default.

    The store returns None for a week never planned; seeding the default is the
    CALLER's decision, made here, so "no week yet" stays distinguishable inside
    the store itself.
    """
    return store.load(user_id, week_start) or default_week(week_start)


def _monday_of(value: date) -> date:
    return value - timedelta(days=value.weekday())


def _replace_slot(plan: WeekPlan, day: str, updated: Slot) -> WeekPlan:
    from dataclasses import replace as dc_replace

    return dc_replace(plan, slots=tuple(updated if s.day == day else s for s in plan.slots))


def _find(plan: WeekPlan, day: str) -> Slot:
    for slot in plan.slots:
        if slot.day.upper() == day.upper():
            return slot
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"error": "no_slot", "message": "There is no slot on that day this week."},
    )


# ── Endpoints ──────────────────────────────────────────────────────────────


@router.get("/week")
def get_week(
    user: CurrentUser,
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """The week plan. Contract §8."""
    week_start = _monday_of(datetime.now(UTC).date())
    store = _store(request, settings)
    plan = _load_or_default(store, user.id, week_start)

    payload = plan.as_dict()
    payload["provenance"] = {
        "agent_label": "Content Intelligence",
        "line": "drafts land 07:00 on slot days",
    }

    if plan.is_paused:
        # An honest nothing, not an empty list. Brief 3a: "Drafts don't
        # accumulate while you're away."
        payload["empty"] = {
            "happened": "No slots this week — your cadence is set to 0.",
            "why": "Drafts don't accumulate while you're away.",
            "next": (
                f"Cadence resumes {plan.paused_until:%a %d %b}"
                if plan.paused_until
                else "Cadence resumes when you set it above 0."
            ),
        }
    return payload


@router.get("/week/{day}")
def get_slot(
    day: str,
    user: CurrentUser,
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """One slot — the editor. Each artifact owns a real URL."""
    week_start = _monday_of(datetime.now(UTC).date())
    store = _store(request, settings)
    slot = _find(_load_or_default(store, user.id, week_start), day)

    return {
        "slot": slot.as_dict(),
        "review": [n.as_dict() for n in _review_notes(slot)],
        # The flow, stated in the payload so no client has to invent the copy —
        # and so "we never auto-post" is asserted by the API itself.
        "flow": [
            {"step": 1, "label": "Review — edit on paper"},
            {"step": 2, "label": "Copy — clipboard, LinkedIn opens"},
            {"step": 3, "label": "Mark published — you say so"},
            {"step": 4, "label": "Logged — streak + Sunday review"},
        ],
        "disclaimer": (
            "Carigma never posts for you. Mark published is you telling us you "
            "shipped it — the streak counts what you shipped, not what we drafted."
        ),
        "skip_reasons": [
            {"value": str(r), "label": r.label, "retrains": r.retrains} for r in SkipReason
        ],
    }


@router.post("/week/{day}/mark-published")
def post_mark_published(
    day: str,
    body: MarkPublishedRequest,
    user: CurrentUser,
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """The honest verb. Free — this is our record-keeping, not their purchase."""
    week_start = _monday_of(datetime.now(UTC).date())
    store = _store(request, settings)
    plan = _load_or_default(store, user.id, week_start)
    slot = _find(plan, day)

    try:
        updated = mark_published(slot, at=body.at or datetime.now(UTC).strftime("%H:%M"))
    except SlotTransitionError as exc:
        logger.info("slot transition refused: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "already_settled", "message": "That slot is already settled."},
        ) from exc

    # One row. `_replace_slot` still rebuilds the in-memory plan for the
    # response, but only the edited day is written — see the module note.
    plan = _replace_slot(plan, slot.day, updated)
    store.save_slot(user.id, week_start, updated)
    return {"slot": updated.as_dict(), "streak": plan.as_dict()["streak"], "charged": 0}


@router.post("/week/{day}/skip")
def post_skip(
    day: str,
    body: SkipRequest,
    user: CurrentUser,
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """Declining is free and it teaches. Never charged, never guilted."""
    week_start = _monday_of(datetime.now(UTC).date())
    store = _store(request, settings)
    plan = _load_or_default(store, user.id, week_start)
    slot = _find(plan, day)

    try:
        updated = skip(slot, reason=body.reason, note=body.note)
    except SlotTransitionError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "reason_required",
                "message": "Tell us why and the next drafts get better.",
            },
        ) from exc

    # One row. `_replace_slot` still rebuilds the in-memory plan for the
    # response, but only the edited day is written — see the module note.
    plan = _replace_slot(plan, slot.day, updated)
    store.save_slot(user.id, week_start, updated)

    return {
        "slot": updated.as_dict(),
        # The streak is HELD. Said explicitly so no client renders a reset.
        "streak": plan.as_dict()["streak"],
        "retrains": bool(body.reason and body.reason.retrains),
        "charged": 0,
    }


@router.post("/week/{day}/regenerate")
async def post_regenerate(
    day: str,
    request: Request,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """New drafts for one slot. 3 credits (§21 Q4) — priced low on purpose.

    Goes through `runs.execute` so a failed or empty generation charges nothing.
    """
    token = (request.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
    client = user_client(settings, token)
    credit_store = SupabaseCreditStore(client)
    run_store = SupabaseRunStore(client)
    action = "regenerate_slot"

    # Replay first: a retried request is not a new one, so it is neither
    # throttled nor charged again.
    if (replayed := replay_if_seen(run_store, user.id, request, agent="content")) is not None:
        return replayed

    enforce_agent_rate_limit(user.id, "content")

    try:
        credits_service.check_affordable(credit_store, user.id, action)
    except InsufficientCredits as exc:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=(
                f"You need {exc.required} credits to redraft this slot. You have {exc.balance}."
            ),
            headers={"X-Error-Code": "insufficient_credits"},
        ) from exc

    week_start = _monday_of(datetime.now(UTC).date())
    store = _store(request, settings)
    plan = _load_or_default(store, user.id, week_start)
    slot = _find(plan, day)
    if slot.state is SlotState.PUBLISHED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "already_published",
                "message": "You've already shipped that one.",
            },
        )

    run = runs_service.new_run(user.id, "content")
    if key := idempotency_key(request):
        run_store.remember_idempotency(user.id, key, run.id)

    def work(reporter: RunReporter) -> dict[str, Any]:
        reporter.step("draft", "Drafting fresh variants")
        # Content Intelligence's generator lands with the agent port. Until it
        # does this raises rather than returning invented copy — an empty or
        # failed run charges nothing, which is the correct behaviour either way.
        raise UpstreamError("Content Intelligence is not wired up yet.")

    settled = await runs_service.execute(
        run, work, credit_store=credit_store, action=action, run_store=run_store
    )

    if settled.status is not runs_service.RunStatus.SUCCEEDED:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "upstream_unavailable",
                "message": "We couldn't draft new variants just now. You weren't charged.",
            },
            headers={"X-Error-Code": "upstream_unavailable"},
        )

    drafts = tuple(
        Draft(index=i + 1, body=d["body"], note=d.get("note"))
        for i, d in enumerate(settled.result.get("drafts", []))
    )
    from dataclasses import replace as dc_replace

    updated = dc_replace(slot, drafts=drafts, state=SlotState.DRAFT_READY)
    store.save_slot(user.id, week_start, updated)

    return {
        "slot": updated.as_dict(),
        "credits": settled.credits.as_dict() if settled.credits else None,
    }


def _review_notes(slot: Slot) -> list[ReviewNote]:
    """What the agent checked.

    Derived from the draft actually in hand — never a fixed list, or the panel
    would claim to have checked something it never read.
    """
    if not slot.drafts:
        return []
    body = slot.drafts[0].body
    first_line = body.split("\n", 1)[0]
    return [
        ReviewNote("?" in first_line, "Hook — question in line 1"),
        ReviewNote(len(body.split()) < 320, "One idea, not three"),
        ReviewNote(body.rstrip().endswith("?"), "Ending asks something"),
    ]


__all__ = ["router"]
