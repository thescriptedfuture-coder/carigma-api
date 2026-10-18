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
from carigma_api.services import content_writer
from carigma_api.services import credits as credits_service
from carigma_api.services import runs as runs_service
from carigma_api.services.ai import call_claude_json
from carigma_api.services.credits import InsufficientCredits
from carigma_api.services.posts import (
    Draft,
    ReviewNote,
    SkipReason,
    Slot,
    SlotState,
    SlotTransitionError,
    WeekPlan,
    mark_published,
    skip,
)
from carigma_api.services.posts_store import SupabasePlanStore
from carigma_api.services.repository import (
    ProfileRepository,
    SupabaseCreditStore,
    user_client,
)
from carigma_api.services.run_store import SupabaseRunStore
from carigma_api.services.runs import RunReporter
from carigma_api.services.schedule import EMPTY, EMPTY_ACTION, Standing, schedule, standing_of
from carigma_api.services.weekly_store import ContractStore, SupabaseContractStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/posts", tags=["posts"])


class SkipRequest(BaseModel):
    reason: SkipReason | None = None
    note: str | None = Field(default=None, max_length=280)


class MarkPublishedRequest(BaseModel):
    #: When the user says they posted it, as an ISO timestamp. Optional — if
    #: they don't say, we stamp now, and either way it is THEIR claim, not our
    #: observation.
    at: str | None = None


def _published_moment(claimed: str | None) -> str:
    """The moment to record, as a full ISO timestamp.

    ## Every "Mark published" in V2 had failed

    This stamped `strftime("%H:%M")` — "13:05" — into
    `content_loop.published_at`, which is `timestamptz`. Postgres refused every
    one ("invalid input syntax for type timestamp with time zone"), the route
    answered 500, and the editor said "We couldn't log that just now". Measured
    on 2026-10-18: zero published rows, ever. The streak — the thing this verb
    exists to feed — could not move.

    A clock time is not a moment: it has no day. The column wanted one, and a
    time alone was only ever going to be a display string.
    """
    if claimed is None:
        return datetime.now(UTC).isoformat()
    try:
        moment = datetime.fromisoformat(claimed.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "bad_time",
                "message": "We couldn't read that time. Leave it out and we'll use now.",
            },
        ) from exc
    return (moment if moment.tzinfo else moment.replace(tzinfo=UTC)).isoformat()


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


def _contracts(request: Request, settings: Settings) -> ContractStore:
    token = (request.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
    return SupabaseContractStore(user_client(settings, token))


def _week(
    request: Request, settings: Settings, user_id: str, week_start: date
) -> tuple[WeekPlan, Standing]:
    """The week as the user signed it (S8), and where its schedule came from.

    This was `store.load(...) or default_week(...)`: Monday and Thursday for
    everyone, whatever they signed. The contract is the schedule now, and the
    stored slots are the work done on it.

    Both reads must succeed. Without the contract there is no schedule to
    show, and without the stored slots the drafts and publishes are missing —
    either one silently absent would render a week that is not theirs.
    """
    try:
        contract = _contracts(request, settings).load(user_id, week_start)
        stored = _store(request, settings).load(user_id, week_start)
    except Exception as exc:
        logger.exception("could not read the week for %s", user_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "read_failed", "message": "We couldn't load your week just now."},
        ) from exc
    return schedule(week_start, contract=contract, stored=stored), standing_of(contract)


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
        detail={
            "error": "no_slot",
            "message": "There is no post on that day in the week you signed.",
        },
    )


def _writer(settings: Settings) -> content_writer.Writer:
    """The drafting call, bound to the real client. A seam, so a test can put a
    dead or a dishonest model behind the route without touching the network."""

    def call(system: str, user: str, max_tokens: int) -> object:
        return call_claude_json(system, user, max_tokens, api_key=settings.anthropic_api_key)

    return call


# ── Endpoints ──────────────────────────────────────────────────────────────


@router.get("/week")
def get_week(
    user: CurrentUser,
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """The week plan. Contract §8."""
    week_start = _monday_of(datetime.now(UTC).date())
    plan, standing = _week(request, settings, user.id, week_start)

    payload = plan.as_dict()
    # Where the schedule came from, so the surface can say "the week you
    # signed" — or why there is no schedule — without re-deriving it.
    payload["standing"] = str(standing)
    payload["provenance"] = {
        "agent_label": "Content Intelligence",
        # This said "drafts land 07:00 on slot days". **Nothing writes a draft
        # on any schedule** — no cron, no worker, nothing. The only writer is
        # `POST /week/{day}/regenerate`, which a person presses. So the line
        # described a system that does not exist, to every user, on the surface
        # where they then found an empty box. Same class as "Your plan is
        # running" for an account with no plan.
        #
        # If a scheduled writer is built, this line becomes true again and the
        # test below is where that is recorded.
        "line": "drafts are written when you ask, 3 credits a slot",
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
    elif not plan.slots:
        # No slots because of the contract, not a pause: say which, and where
        # to change it.
        payload["empty"] = {**EMPTY[standing], "action": EMPTY_ACTION[standing]}
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
    plan, _standing = _week(request, settings, user.id, week_start)
    slot = _find(plan, day)

    return {
        "slot": slot.as_dict(),
        "review": [n.as_dict() for n in _review_notes(slot)],
        # Why this post is worth the user's ten minutes. Reported from the
        # first real use: a page with three drafts and no word on what they
        # are FOR reads as busywork. Stated, not inflated — no reach figures,
        # no promises about recruiters; only what is true of every profile.
        "why": (
            "When someone opens your LinkedIn profile, your recent posts are on it — "
            "what you know, in your own words. Each draft here is written from your profile."
        ),
        # What to do, and WHERE. In the payload so no client invents the copy,
        # and so "we never auto-post" is asserted by the API itself.
        #
        # This was four terse labels at the bottom of the page — "Copy —
        # clipboard, LinkedIn opens" among them, while the button beside it
        # only copied. Each step now says what to press and what happens.
        "flow": _flow(len(slot.drafts)),
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
    plan, _standing = _week(request, settings, user.id, week_start)
    slot = _find(plan, day)

    try:
        updated = mark_published(slot, at=_published_moment(body.at))
    except SlotTransitionError as exc:
        logger.info("slot transition refused: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "already_settled",
                "message": "That one is already marked published or skipped.",
            },
        ) from exc

    # One row. `_replace_slot` still rebuilds the in-memory plan for the
    # response, but only the edited day is written — see the module note.
    plan = _replace_slot(plan, slot.day, updated)
    try:
        store.save_slot(user.id, week_start, updated)
    except Exception as exc:
        # A refused write is not a published post. Said as what it is, rather
        # than a bare 500 the surface can only call "something went wrong".
        logger.exception("could not record %s as published for %s", slot.day, user.id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "not_saved",
                "message": "We couldn't record that just now. Nothing changed — try again.",
            },
        ) from exc
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
    plan, _standing = _week(request, settings, user.id, week_start)
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
    """Drafts for one slot — the first ones, or fresh ones. 3 credits (§21 Q4).

    Goes through `runs.execute` so a failed or empty generation charges nothing.
    "Empty" includes a reply whose every draft failed `content_writer.check` —
    an invented figure or a blank to fill is not a draft, and the user is not
    charged for the model's mistake.
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

    # Structural refusals come FIRST, before any question about credits.
    #
    # This used to sit after `check_affordable`, and the test beside it is
    # called "refused before any work" — it passed only because an unreadable
    # balance ran FREE, so the credit check never refused anything. Making
    # credits strict surfaced the real order: **you should not have to be able
    # to afford something to be told it is impossible.**
    week_start = _monday_of(datetime.now(UTC).date())
    store = _store(request, settings)
    plan, _standing = _week(request, settings, user.id, week_start)
    slot = _find(plan, day)
    if slot.state is SlotState.PUBLISHED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "already_published",
                "message": "You've already shipped that one.",
            },
        )

    # The second structural refusal: nothing to write FROM. A profile with no
    # experience, skills or about text would get a stranger's career, and the
    # rule is that every claim in a draft is one the user made first.
    try:
        previous = store.load(user.id, week_start - timedelta(days=7))
    except Exception:
        # Last week only sharpens the brief — what was declined, what was used.
        # Its absence makes the drafts less informed, not wrong.
        logger.exception("could not read last week's plan for %s", user.id)
        previous = None
    try:
        profile = ProfileRepository(client).load(user.id)
    except Exception as exc:
        # Unreadable is not "empty". Writing from an empty profile would be
        # writing from nothing; refusing costs a retry, and nothing else.
        logger.exception("could not read the profile for %s", user.id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "profile_unavailable",
                "message": (
                    "We couldn't read your profile just now, and drafts are written "
                    "from it. You weren't charged and nothing ran."
                ),
            },
        ) from exc
    try:
        brief = content_writer.brief_for(profile, slot, week=plan, previous=previous)
    except content_writer.NothingToWriteFrom as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "nothing_to_write_from",
                "message": (
                    "Add your experience or skills to your profile first. Drafts are "
                    "written from what you've told us, and we won't invent a career "
                    "to fill the page. Nothing was charged."
                ),
            },
        ) from exc

    try:
        credits_service.check_affordable(
            credit_store, user.id, action, enforced=settings.credits_enforced
        )
    except credits_service.CreditsUnavailable as exc:
        # We could not read the balance, so we cannot account for this run.
        # Refusing is not "charging on failure" — nothing is charged and no
        # work starts. Before this, an unreadable balance ran the work FREE.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "credits_unavailable",
                "message": (
                    "We couldn't check your credits just now, so you weren't "
                    "charged and nothing ran. Try again in a moment."
                ),
            },
            headers={"X-Error-Code": "credits_unavailable"},
        ) from exc
    except InsufficientCredits as exc:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=(
                f"You need {exc.required} credits to redraft this slot. You have {exc.balance}."
            ),
            headers={"X-Error-Code": "insufficient_credits"},
        ) from exc

    run = runs_service.new_run(user.id, "content")
    if key := idempotency_key(request):
        run_store.remember_idempotency(user.id, key, run.id)

    def work(reporter: RunReporter) -> dict[str, Any]:
        reporter.step("draft", f"Writing {content_writer.VARIANTS} drafts for {slot.day}")
        drafts = content_writer.write(brief, writer=_writer(settings))
        reporter.step("check", "Checking each against what your profile says")
        # `{}` is the one shape `runs.execute` will not charge for. Every draft
        # dropped by the check is the model's failure, not the user's purchase.
        return {"drafts": drafts} if drafts else {}

    settled = await runs_service.execute(
        run, work, credit_store=credit_store, action=action, run_store=run_store
    )

    if settled.status is runs_service.RunStatus.EMPTY:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "nothing_usable",
                "message": (
                    "Every draft came back with something we won't put under your "
                    "name — a figure your profile doesn't contain, or a blank to fill "
                    "in — so none were kept. You weren't charged."
                ),
            },
            headers={"X-Error-Code": "nothing_usable"},
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


def _flow(drafts: int) -> list[dict[str, Any]]:
    """The three steps from a draft to a post, with the count taken from the
    drafts actually there — the check drops some, so "three" is not a given.
    No "above" or "below": where the drafts sit is the surface's business."""
    which = (
        f"{drafts} drafts are ready"
        if drafts > 1
        else "Your draft is ready"
        if drafts == 1
        else "Write the drafts first"
    )
    return [
        {
            "step": 1,
            "label": "Pick a draft and make it yours",
            "detail": (
                f"{which} — choose the one that sounds most like you, and edit it on the "
                "paper. It's your post now."
            ),
        },
        {
            "step": 2,
            "label": "Copy it and post it on LinkedIn",
            "detail": (
                "Copy & open LinkedIn copies the text and opens LinkedIn in a new tab. "
                "Start a post there, paste it (Ctrl+V) and post it yourself — Carigma never "
                "posts for you."
            ),
        },
        {
            "step": 3,
            "label": "Come back and mark it published — you say so",
            "detail": (
                "That's what your streak and your Sunday review count: what you shipped, "
                "not what we drafted."
            ),
        },
    ]


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
