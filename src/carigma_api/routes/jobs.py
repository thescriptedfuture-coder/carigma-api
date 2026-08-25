"""The jobs feed. Roadmap §9.

    GET  /jobs/feed                free — reads what has been scanned
    GET  /jobs/{id}                free
    GET  /jobs/{id}/apply-kit      free — returns the STORED kit, or 404
    POST /jobs/{id}/not-for-me     free — records a decision

Every route here is free. Nothing on this path calls a provider or an agent, so
there is no work to charge for: the scan that populates `jobs_feed` is V1's
today and will be its own action when V2 owns it.

The web has been calling these four against MSW since P4. They did not exist.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from carigma_api.auth.dependencies import CurrentUser
from carigma_api.config import Settings, get_settings
from carigma_api.services.jobs.feed import DEFAULT_STATUSES, present_feed, present_job
from carigma_api.services.repository import _as_rows, user_client

logger = logging.getLogger(__name__)

router = APIRouter(tags=["jobs"])

TABLE = "jobs_feed"


class NotForMe(BaseModel):
    #: Free text, because the useful reasons are the ones we did not think of.
    #: V1 stored strings like "Salary too low" and they are the signal.
    reason: str = Field(min_length=1, max_length=200)


def _db(request: Request, settings: Settings) -> Any:
    token = (request.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
    return user_client(settings, token)


def _unavailable(what: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={"error": "read_failed", "message": f"We couldn't load {what} just now."},
    )


@router.get("/jobs/feed")
def get_jobs_feed(
    request: Request,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """Every job we have surfaced for THIS user.

    Scoped by `user_id` as well as by RLS. The filter is defence in depth, not
    the boundary — the boundary is the database, and this reads through the
    caller's own token so `auth.uid()` is real.
    """
    db = _db(request, settings)
    try:
        res = (
            db.table(TABLE)
            .select("*")
            .eq("user_id", user.id)
            .in_("status", list(DEFAULT_STATUSES))
            .order("match_score", desc=True)
            .execute()
        )
    except Exception as exc:
        logger.exception("could not read the jobs feed for %s", user.id)
        raise _unavailable("your job matches") from exc

    return present_feed(_as_rows(res.data if res else None))


def _one(db: Any, user_id: str, job_id: int) -> dict[str, Any]:
    try:
        res = db.table(TABLE).select("*").eq("id", job_id).eq("user_id", user_id).execute()
    except Exception as exc:
        logger.exception("could not read job %s for %s", job_id, user_id)
        raise _unavailable("that job") from exc

    rows = _as_rows(res.data if res else None)
    if not rows:
        # A job belonging to someone else is a 404, not a 403: telling a caller
        # that an id exists but is not theirs is itself a disclosure.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "not_found", "message": "We don't have that job."},
        )
    return rows[0]


@router.get("/jobs/{job_id}")
def get_job(
    job_id: int,
    request: Request,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    row = _one(_db(request, settings), user.id, job_id)
    return present_job(row, today=datetime.now(UTC).date())


@router.get("/jobs/{job_id}/apply-kit")
def get_apply_kit(
    job_id: int,
    request: Request,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """The stored kit, or an honest 404.

    Nine of the ninety-three stored rows have one. This returns what was saved
    and never generates on read — a kit conjured here would be unpaid work
    presented as a record of work already done.
    """
    row = _one(_db(request, settings), user.id, job_id)
    kit = row.get("apply_kit")
    if not isinstance(kit, dict) or not kit:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "no_kit",
                "message": "No apply kit has been built for this job yet.",
            },
        )
    return {
        **kit,
        "created_at": row.get("kit_created_at"),
        # Shipped in the payload rather than hard-coded in a component, so a
        # surface cannot render the kit without it.
        "disclaimer": "Review and adjust before sending — Carigma drafts, you verify.",
    }


@router.post("/jobs/{job_id}/not-for-me")
def dismiss_job(
    job_id: int,
    body: NotForMe,
    request: Request,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """Record why this one was wrong. Free — it is the user teaching us.

    V1's bug B was that the reason did not save, which made the whole gesture
    theatre. The response echoes the stored reason so the caller can tell the
    difference between "recorded" and "accepted and dropped".
    """
    db = _db(request, settings)
    _one(db, user.id, job_id)

    try:
        res = (
            db.table(TABLE)
            .update({"status": "dismissed", "dismiss_reason": body.reason.strip()})
            .eq("id", job_id)
            .eq("user_id", user.id)
            .execute()
        )
    except Exception as exc:
        logger.exception("could not dismiss job %s for %s", job_id, user.id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "save_failed",
                "message": "We couldn't save that just now. The job is unchanged.",
            },
        ) from exc

    rows = _as_rows(res.data if res else None)
    stored = rows[0] if rows else {}
    return {
        "id": job_id,
        "status": stored.get("status") or "dismissed",
        # Read back from the row, not echoed from the request. The point of the
        # fix is that the reason is stored, and only the row can prove that.
        "dismiss_reason": stored.get("dismiss_reason"),
    }


__all__ = ["router"]
