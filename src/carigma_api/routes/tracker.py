"""The pipeline. Spec 3.5 — "the retention spine. Where the job search lives."

    GET    /tracker                  the board, five columns
    GET    /tracker/{id}             one application and its timeline
    POST   /tracker                  add one, by hand or from a job
    PATCH  /tracker/{id}             move stage, set a date, close with a reason
    POST   /tracker/{id}/note        log an update (spec 9.4)
    DELETE /tracker/{id}             remove one

Everything here is free. Recording what you did is not a run.

## Built on V1's table, because V1's table is the one with rows in it

`job_applications`, not `tracker` — see `services/applications.py` for what
counting the rows found. Every write goes through the CALLER's client so RLS
applies; nothing here uses the service role.

## Events are written beside stage changes, never instead of them

A stage move writes the row AND an `application_events` entry, in that order.
If the event write fails the move still stands: losing the timeline entry is
worse than losing nothing, and far better than refusing a move the user made.
The failure is logged, never surfaced as an error for an action that worked.

**No event is written for a move that did not happen.** `PATCH` with the stage
it already has is a no-op, because a timeline of "moved to Applied" five times
is a timeline nobody can read.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from carigma_api.auth.dependencies import CurrentUser
from carigma_api.config import Settings, get_settings
from carigma_api.services.applications import (
    CLOSED_REASONS,
    EVENTS,
    JOB_STAGES,
    STAGE_LABELS,
    TABLE,
    board,
    event_for,
    normalise_stage,
    present,
    summary,
    valid_interview_date,
)
from carigma_api.services.repository import user_client

logger = logging.getLogger(__name__)

router = APIRouter(tags=["tracker"])


class NewApplication(BaseModel):
    company: str = Field(min_length=1, max_length=200)
    role: str = Field(min_length=1, max_length=200)
    #: Defaults to `saved`, which is what adding from the jobs feed means.
    stage: str = Field(default="saved")
    link: str | None = Field(default=None, max_length=2000)
    salary: str | None = Field(default=None, max_length=120)
    notes: str | None = Field(default=None, max_length=4000)


class Move(BaseModel):
    """Every field optional — this is a patch, and a patch of nothing is a 400.

    An OPTIONAL BODY IS NOT AN OPTIONAL FIELD, and the reverse trap is here
    too: making every field optional means `{}` validates and silently does
    nothing. So the handler refuses an empty patch rather than answering 200
    to a request that changed no rows.
    """

    stage: str | None = None
    interview_on: str | None = None
    closed_reason: str | None = None
    notes: str | None = Field(default=None, max_length=4000)


class Note(BaseModel):
    note: str = Field(min_length=1, max_length=300)


def _db(request: Request, settings: Settings) -> Any:
    token = (request.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
    return user_client(settings, token)


def _unavailable(what: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={"error": "read_failed", "message": f"We couldn't load {what} just now."},
    )


def _rows(db: Any, user_id: str) -> list[dict[str, Any]]:
    res = db.table(TABLE).select("*").eq("user_id", user_id).execute()
    return res.data if res and isinstance(res.data, list) else []


def _one(db: Any, user_id: str, app_id: str) -> dict[str, Any]:
    res = db.table(TABLE).select("*").eq("user_id", user_id).eq("id", app_id).execute()
    rows = res.data if res and isinstance(res.data, list) else []
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "no_application", "message": "We don't have that application."},
        )
    return dict(rows[0])


def _log(db: Any, user_id: str, app_id: str, event: dict[str, str]) -> None:
    """The timeline entry beside a change that already happened.

    Never raises. The move is done; failing the request now would tell the user
    their action failed when it did not, and they would do it twice.
    """
    try:
        db.table(EVENTS).insert({"user_id": user_id, "application_id": app_id, **event}).execute()
    except Exception:
        logger.exception("could not log %s on application %s", event.get("event_type"), app_id)


@router.get("/tracker")
def get_board(
    request: Request,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """Five columns, every one of them present even when empty."""
    try:
        rows = _rows(_db(request, settings), user.id)
    except Exception as exc:
        logger.exception("could not read the pipeline for %s", user.id)
        raise _unavailable("your pipeline") from exc

    now = datetime.now(UTC)
    return {
        "columns": board(rows, now=now),
        "summary": summary(rows, now=now),
        "closed_reasons": list(CLOSED_REASONS),
    }


@router.get("/tracker/{app_id}")
def get_application(
    app_id: str,
    request: Request,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """One application, with the events we own.

    Spec 9.4's guardrail: every row here is something the product did or the
    user logged. There is no "recruiter viewed your profile" — we cannot know
    it, and a timeline that invents one entry is a timeline nobody can trust.
    """
    db = _db(request, settings)
    row = _one(db, user.id, app_id)

    events: list[dict[str, Any]] = []
    try:
        res = (
            db.table(EVENTS)
            .select("event_type,note,created_at")
            .eq("user_id", user.id)
            .eq("application_id", app_id)
            .order("created_at", desc=True)
            .limit(100)
            .execute()
        )
        events = res.data if res and isinstance(res.data, list) else []
    except Exception:
        # The application is readable; the timeline is not. Showing the row
        # without its history beats showing nothing, but the client must be
        # able to tell "no events yet" from "we could not read them".
        logger.exception("could not read the timeline for %s", app_id)
        return {**present(row, now=datetime.now(UTC)), "events": None}

    return {**present(row, now=datetime.now(UTC)), "events": events}


@router.post("/tracker", status_code=status.HTTP_201_CREATED)
def add_application(
    body: NewApplication,
    request: Request,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """Add one — from the jobs feed, or typed in by hand."""
    stage = normalise_stage(body.stage)
    db = _db(request, settings)
    payload = {
        "user_id": user.id,
        "company": body.company,
        "role": body.role,
        "status": stage,
        "link": body.link,
        "salary": body.salary,
        "notes": body.notes,
    }
    try:
        res = db.table(TABLE).insert(payload).execute()
    except Exception as exc:
        logger.exception("could not add an application for %s", user.id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "write_failed", "message": "We couldn't save that just now."},
        ) from exc

    rows = res.data if res and isinstance(res.data, list) else []
    row = dict(rows[0]) if rows else payload
    if row.get("id"):
        _log(db, user.id, str(row["id"]), event_for(stage))
    return present(row, now=datetime.now(UTC))


@router.patch("/tracker/{app_id}")
def move_application(
    app_id: str,
    body: Move,
    request: Request,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """Move a stage, set an interview date, or close with a reason."""
    db = _db(request, settings)
    current = _one(db, user.id, app_id)
    patch: dict[str, Any] = {}

    if body.stage is not None:
        if body.stage not in JOB_STAGES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error": "unknown_stage",
                    "message": f"A stage is one of: {', '.join(STAGE_LABELS.values())}.",
                },
            )
        patch["status"] = body.stage

    if body.interview_on is not None:
        when = valid_interview_date(body.interview_on)
        if when is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"error": "bad_date", "message": "That isn't a date we can read."},
            )
        # A DATE, because the column is one. Storing a timestamp here is how
        # the comparison in `next_interview` started raising.
        patch["interview_on"] = when.isoformat()

    if body.closed_reason is not None:
        if body.closed_reason not in CLOSED_REASONS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error": "unknown_reason",
                    "message": f"A reason is one of: {', '.join(CLOSED_REASONS)}.",
                },
            )
        patch["closed_reason"] = body.closed_reason

    if body.notes is not None:
        patch["notes"] = body.notes

    if not patch:
        # `{}` validates against a model of optional fields and changes nothing.
        # Answering 200 would report success for a request that did no work.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "nothing_to_change", "message": "That request changed nothing."},
        )

    patch["updated_at"] = datetime.now(UTC).isoformat()
    try:
        db.table(TABLE).update(patch).eq("user_id", user.id).eq("id", app_id).execute()
    except Exception as exc:
        logger.exception("could not update application %s", app_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "write_failed", "message": "We couldn't save that just now."},
        ) from exc

    moved = "status" in patch and normalise_stage(current.get("status")) != patch["status"]
    if moved:
        # Only a real move earns an event. A timeline that says "moved to
        # Applied" five times is a timeline nobody reads.
        _log(db, user.id, app_id, event_for(patch["status"], reason=body.closed_reason))

    return present(_one(db, user.id, app_id), now=datetime.now(UTC))


@router.post("/tracker/{app_id}/followed-up", status_code=status.HTTP_201_CREATED)
def mark_followed_up(
    app_id: str,
    request: Request,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """ "Follow up?" was a chip that said something and did nothing.

    A nudge with no action beside it is a reminder the product repeats at you
    forever — the clock only moves when a stage does, so an application you DID
    chase goes on asking to be chased.

    This records the chase: an event on the timeline, and `updated_at` touched,
    which is what `days_in_stage` is measured from. The nudge clears because
    the thing it was nudging about happened.
    """
    db = _db(request, settings)
    _one(db, user.id, app_id)
    now = datetime.now(UTC).isoformat()
    try:
        db.table(TABLE).update({"updated_at": now}).eq("user_id", user.id).eq(
            "id", app_id
        ).execute()
    except Exception as exc:
        logger.exception("could not mark %s followed up", app_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "write_failed", "message": "We couldn't save that just now."},
        ) from exc

    _log(db, user.id, app_id, {"event_type": "note", "note": "Followed up"})
    return present(_one(db, user.id, app_id), now=datetime.now(UTC))


@router.post("/tracker/{app_id}/note", status_code=status.HTTP_201_CREATED)
def add_note(
    app_id: str,
    body: Note,
    request: Request,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """ "Log an update" — spec 9.4's user-written timeline entry."""
    db = _db(request, settings)
    _one(db, user.id, app_id)
    try:
        db.table(EVENTS).insert(
            {
                "user_id": user.id,
                "application_id": app_id,
                "event_type": "note",
                "note": body.note,
            }
        ).execute()
    except Exception as exc:
        logger.exception("could not log a note on %s", app_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "write_failed", "message": "We couldn't save that note just now."},
        ) from exc
    return {"logged": True}


@router.delete("/tracker/{app_id}")
def remove_application(
    app_id: str,
    request: Request,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """Remove one. The events go with it — an orphan timeline is not a record."""
    db = _db(request, settings)
    _one(db, user.id, app_id)
    try:
        db.table(EVENTS).delete().eq("user_id", user.id).eq("application_id", app_id).execute()
        db.table(TABLE).delete().eq("user_id", user.id).eq("id", app_id).execute()
    except Exception as exc:
        logger.exception("could not remove application %s", app_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "write_failed", "message": "We couldn't remove that just now."},
        ) from exc
    return {"removed": True}


__all__ = ["router"]
