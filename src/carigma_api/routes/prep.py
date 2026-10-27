"""Interview prep, per application. Roadmap S14; V1's tracker prep, ported.

    GET  /tracker/{id}/prep    free — the stored kit, or `prep: null`
    POST /tracker/{id}/prep    12 credits — Interview Intelligence builds one

Until this existed, V2's prep page read "Interview prep · not built yet", and
Today's top card, an interview inside 72 hours, led to an application with
nothing to prepare from. V1 built kits from the tracker's interview stage, and
`interview_preps` holds the ones it built (one per application, unique on
`application_id`). V2 reads those as they are and writes the same shape.

Its own module rather than `tracker.py`, whose promise is that recording what
you did is free. This is a run.

## Rebuilding is allowed, and charged

V1 offered "Rebuild my prep" at full price, because a kit is built from the
profile and the role, and either can change. The same here: the page shows the
price on the button. A rebuild replaces the kit only once the new one is saved;
a failed rebuild leaves the old kit, and the run is free.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from carigma_api.auth.dependencies import CurrentUser
from carigma_api.config import Settings, get_settings
from carigma_api.routes._guards import (
    enforce_agent_rate_limit,
    idempotency_key,
    refuse_if_session_expiring,
    replay_if_seen,
)
from carigma_api.services import credits as credits_service
from carigma_api.services import runs as runs_service
from carigma_api.services.agents import interview_coach
from carigma_api.services.ai import call_claude_json
from carigma_api.services.applications import EVENTS, TABLE
from carigma_api.services.constants import CREDIT_COSTS
from carigma_api.services.credits import InsufficientCredits
from carigma_api.services.repository import ProfileRepository, SupabaseCreditStore, user_client
from carigma_api.services.run_store import SupabaseRunStore, replay_payload
from carigma_api.services.runs import RunNotStored, RunReporter

logger = logging.getLogger(__name__)

router = APIRouter(tags=["prep"])

PREPS = "interview_preps"
ACTION = "run_interview"


class PrepRequest(BaseModel):
    #: `true`: answer 202 with the run at once; the page polls
    #: `GET /agents/runs/{id}`. A kit is ~4,500 tokens of output, longer than
    #: the CV that already runs in the background (0.076).
    background: bool = False


def _db(request: Request, settings: Settings) -> Any:
    token = (request.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
    return user_client(settings, token)


def _unavailable(what: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={"error": "read_failed", "message": f"We couldn't load {what} just now."},
    )


def _application(db: Any, user_id: str, app_id: str) -> dict[str, Any]:
    try:
        res = db.table(TABLE).select("*").eq("user_id", user_id).eq("id", app_id).execute()
    except Exception as exc:
        logger.exception("could not read application %s for %s", app_id, user_id)
        raise _unavailable("that application") from exc
    rows = res.data if res and isinstance(res.data, list) else []
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "no_application", "message": "We don't have that application."},
        )
    return dict(rows[0])


def _role(app: dict[str, Any]) -> str:
    """What the kit prepares for: V1's `"{role} at {company}"`."""
    return f"{app.get('role') or ''} at {app.get('company') or ''}".strip()


def _payload(app_id: str, prep: dict[str, Any] | None, built_at: Any) -> dict[str, Any]:
    """The kit as the client reads it — stored and freshly built alike."""
    return {
        "application_id": app_id,
        "prep": prep,
        "built_at": built_at if prep else None,
        "cost": CREDIT_COSTS[ACTION],
        "disclaimer": interview_coach.DISCLAIMER,
    }


def _writer(settings: Settings) -> interview_coach.Writer:
    """The model call, bound to the real client. A seam for the tests."""

    def call(system: str, user: str, max_tokens: int) -> object:
        return call_claude_json(system, user, max_tokens, api_key=settings.anthropic_api_key)

    return call


@router.get("/tracker/{app_id}/prep")
def get_prep(
    app_id: str,
    request: Request,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """The stored kit, or `prep: null`. Never generates on read."""
    db = _db(request, settings)
    app = _application(db, user.id, app_id)
    try:
        res = (
            db.table(PREPS)
            .select("prep,created_at")
            .eq("user_id", user.id)
            .eq("application_id", app_id)
            .execute()
        )
    except Exception as exc:
        # A failed read is not "no kit": the page would offer to build, and
        # charge for, a kit the person already has.
        logger.exception("could not read the prep for %s", app_id)
        raise _unavailable("your prep") from exc
    rows = res.data if res and isinstance(res.data, list) else []
    if not rows:
        return _payload(app_id, None, None)
    # Kits V1 built are read as they are, held to the shape the page renders.
    # One usable question is enough to show what was paid for.
    kit = interview_coach.kit_from(rows[0].get("prep"), role=_role(app), minimum=1)
    if kit is None:
        logger.error("unreadable prep payload on %s for application %s", PREPS, app_id)
        raise _unavailable("your prep")
    return _payload(app_id, kit, rows[0].get("created_at"))


@router.post("/tracker/{app_id}/prep")
async def build_prep(
    app_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
    body: PrepRequest | None = None,
) -> Any:
    """Interview Intelligence: likely questions with model answers from the
    person's own background, questions to ask back, mistakes to avoid. 12
    credits, and nothing when nothing usable comes back.

    Order, as everywhere: replay, rate limit, the structural refusals (no such
    application, nothing to prepare from), then credits.
    """
    client = _db(request, settings)
    credit_store = SupabaseCreditStore(client)
    run_store = SupabaseRunStore(client)

    if (replayed := replay_if_seen(run_store, user.id, request, agent="interview")) is not None:
        return replayed

    background = bool(body and body.background)
    if background:
        refuse_if_session_expiring(user)

    enforce_agent_rate_limit(user.id, "interview")

    app = _application(client, user.id, app_id)
    role = _role(app)

    try:
        profile = ProfileRepository(client).load(user.id)
    except Exception as exc:
        logger.exception("could not read the profile for %s", user.id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "profile_unavailable",
                "message": (
                    "We couldn't read your profile just now, and the prep is built from it. "
                    "You weren't charged and nothing ran."
                ),
            },
        ) from exc
    try:
        interview_coach.require_background(profile)
    except interview_coach.NothingToPrepFrom as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "nothing_to_prep_from",
                "message": (
                    "Add your experience to your profile first. Model answers are built from "
                    "what you've done, and we won't invent it. Nothing was charged."
                ),
            },
        ) from exc

    try:
        credits_service.check_affordable(
            credit_store, user.id, ACTION, enforced=settings.credits_enforced
        )
    except credits_service.CreditsUnavailable as exc:
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
            detail=f"Interview prep costs {exc.required} credits. You have {exc.balance}.",
            headers={"X-Error-Code": "insufficient_credits"},
        ) from exc

    run = runs_service.new_run(user.id, "interview")
    # Which application, so its page — and only it — can find the run again.
    run.subject = {"application_id": app_id}
    if key := idempotency_key(request):
        run_store.remember_idempotency(user.id, key, run.id)
    built_at = datetime.now(UTC).isoformat()

    def work(reporter: RunReporter) -> dict[str, Any]:
        reporter.step("read", f"Studying the {role} role and your background")
        kit = interview_coach.write(profile, role, writer=_writer(settings))
        if kit is None:
            # `{}` is the shape `runs.execute` will not charge for.
            return {}
        reporter.step("save", "Saving it to this application")
        # Saved INSIDE the run: a kit that could not be kept is a failed run,
        # and a failed run is not charged. V1 spent only after the save too.
        saved = (
            client.table(PREPS)
            .upsert(
                {
                    "user_id": user.id,
                    "application_id": app_id,
                    "prep": kit,
                    "created_at": built_at,
                },
                on_conflict="application_id",
            )
            .execute()
        )
        if not (saved and saved.data):
            # RLS refuses a write by touching nothing, without an error.
            raise RuntimeError(f"the prep for application {app_id} was not saved")
        reporter.complete_step()
        try:
            client.table(EVENTS).insert(
                {
                    "user_id": user.id,
                    "application_id": app_id,
                    # V1's event name, so the timeline reads one vocabulary.
                    "event_type": "prep_built",
                    "note": "Interview prep kit generated",
                }
            ).execute()
        except Exception:
            # The kit is saved and paid for; a missing timeline line must not
            # turn that into a failed run.
            logger.exception("could not log prep_built on application %s", app_id)
        return _payload(app_id, kit, built_at)

    try:
        holder = runs_service.begin(run, run_store)
    except RunNotStored as exc:
        if background:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "error": "run_not_started",
                    "message": "We couldn't start your prep just now. Nothing was charged.",
                },
                headers={"X-Error-Code": "run_not_started"},
            ) from exc
        holder = None
    if holder is not None:
        return replay_payload(holder)

    if background:
        background_tasks.add_task(
            runs_service.settle_detached,
            run,
            work,
            credit_store=credit_store,
            action=ACTION,
            run_store=run_store,
        )
        return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content=run.as_dict())

    settled = await runs_service.execute(
        run, work, credit_store=credit_store, action=ACTION, run_store=run_store
    )
    if settled.status is runs_service.RunStatus.EMPTY:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "nothing_usable",
                "message": (
                    "The prep came back too thin to be worth keeping, so it wasn't saved. "
                    "You weren't charged."
                ),
            },
            headers={"X-Error-Code": "nothing_usable"},
        )
    if settled.status is not runs_service.RunStatus.SUCCEEDED:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "upstream_unavailable",
                "message": "We couldn't build your prep just now. You weren't charged.",
            },
            headers={"X-Error-Code": "upstream_unavailable"},
        )
    return {
        **settled.result,
        "credits": settled.credits.as_dict() if settled.credits else None,
    }
