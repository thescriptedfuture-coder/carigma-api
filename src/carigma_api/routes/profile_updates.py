"""Incremental profile changes over HTTP. Contract §5b.

Three endpoints, and the split between them is the whole design:

    POST /profile/updates        add a change, get the PROPOSAL back   FREE
    GET  /profile/updates/open   the pending offer, for the surface     FREE
    POST /profile/updates/{id}/decide   yes or no, once                FREE

Nothing here charges. Approving hands the named agents to the normal run
protocol, which charges exactly as it does for any other run — so the credit
rule keeps living in one place (`services.runs.execute`) rather than growing a
second implementation here.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from carigma_api.auth.dependencies import CurrentUser
from carigma_api.config import Settings, get_settings
from carigma_api.services.profile_updates import (
    AlreadyDecided,
    ChangeRecord,
    Kind,
    Magnitude,
    Operation,
    State,
    UnknownField,
    build_proposal,
    clean_payload,
    decide,
    magnitude_of,
)
from carigma_api.services.repository import user_client

logger = logging.getLogger(__name__)

router = APIRouter(tags=["profile-updates"])

TABLE = "profile_updates"


class ChangeRequest(BaseModel):
    kind: Kind
    operation: Operation = Operation.ADD
    #: Validated against ALLOWED_FIELDS, not accepted wholesale. Free text in a
    #: field every agent reads as fact is how Carigma ends up stating something
    #: the user never said.
    payload: dict[str, Any] = Field(default_factory=dict)


class DecisionRequest(BaseModel):
    approved: bool


def _db(request: Request, settings: Settings) -> Any:
    token = (request.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
    return user_client(settings, token)


def _record_from_row(row: dict[str, Any]) -> ChangeRecord:
    return ChangeRecord(
        id=int(row["id"]),
        user_id=str(row["user_id"]),
        kind=Kind(row["kind"]),
        operation=Operation(row["operation"]),
        payload=row.get("payload") or {},
        magnitude=Magnitude(row["magnitude"]),
        proposal=row.get("proposal") or {},
        state=State(row["state"]),
        decided_at=row.get("decided_at"),
        applied_at=row.get("applied_at"),
    )


@router.post("/profile/updates", status_code=status.HTTP_201_CREATED)
def post_change(
    body: ChangeRequest,
    request: Request,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """Record a structured change and return what we propose doing about it.

    **Free.** Charging to notice a change would suppress exactly the behaviour
    we want, so nothing on this path touches credits.

    Nothing is applied. The response is an OFFER — the co-pilot model, not a
    regenerate button the user has to find.
    """
    try:
        payload = clean_payload(body.kind, body.payload)
    except UnknownField as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "unknown_field", "message": str(exc)},
        ) from exc

    proposal = build_proposal(body.kind, payload, body.operation)

    try:
        created = (
            _db(request, settings)
            .table(TABLE)
            .insert(
                {
                    "user_id": user.id,
                    "kind": str(body.kind),
                    "operation": str(body.operation),
                    "payload": payload,
                    "magnitude": str(magnitude_of(body.kind, body.operation)),
                    "proposal": proposal.as_dict(),
                }
            )
            .execute()
            .data
        )
    except Exception as exc:
        logger.exception("could not record profile change for %s", user.id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "save_failed",
                # Says what is still true. Implying the change landed when it
                # did not would leave the user believing their profile has an
                # entry it does not have.
                "message": (
                    "We couldn't record that just now, so nothing was added. Try again in a moment."
                ),
            },
        ) from exc

    row = created[0] if created else {}
    return {"id": row.get("id"), "proposal": proposal.as_dict(), "state": str(State.PROPOSED)}


@router.get("/profile/updates/open")
def get_open(
    request: Request,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """The pending offer, if there is one.

    Returns `{"change": null}` rather than 404 for "nothing pending": an empty
    inbox is a normal state, not an error, and a surface should not have to
    catch an exception to render the quiet case.
    """
    try:
        rows = (
            _db(request, settings)
            .table(TABLE)
            .select("*")
            .eq("user_id", user.id)
            .eq("state", str(State.PROPOSED))
            .order("created_at", desc=True)
            .limit(1)
            .execute()
            .data
        ) or []
    except Exception:
        logger.exception("could not read open profile changes for %s", user.id)
        # ProviderUnavailable is not empty. Saying "nothing pending" when we
        # could not look would hide a proposal the user is waiting on.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "read_failed",
                "message": "We couldn't check for pending changes just now.",
            },
        ) from None

    if not rows:
        return {"change": None}
    return {"change": _record_from_row(rows[0]).as_dict()}


@router.post("/profile/updates/{change_id}/decide")
def post_decision(
    change_id: int,
    body: DecisionRequest,
    request: Request,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """Approve or decline. Once.

    Re-deciding is a 409, not an overwrite — the second answer would silently
    replace the record of what the user actually agreed to. The weekly contract
    makes the same refusal for the same reason.

    Approval does NOT run the agents here. It returns the agent list, and the
    client starts them through the normal run protocol, so the credit rule
    stays in one place.
    """
    db = _db(request, settings)
    try:
        rows = (
            db.table(TABLE).select("*").eq("id", change_id).eq("user_id", user.id).execute().data
        ) or []
    except Exception as exc:
        logger.exception("could not load change %s", change_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "read_failed", "message": "We couldn't load that change."},
        ) from exc

    if not rows:
        # Scoped to this user, so someone else's id is indistinguishable from a
        # missing one — which is the correct answer to give either way.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "not_found", "message": "That change is no longer pending."},
        )

    record = _record_from_row(rows[0])
    now = datetime.now(UTC).isoformat()
    try:
        decide(record, approved=body.approved, now_iso=now)
    except AlreadyDecided as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "already_decided", "message": str(exc)},
        ) from exc

    try:
        db.table(TABLE).update({"state": str(record.state), "decided_at": now}).eq(
            "id", change_id
        ).eq("user_id", user.id).execute()
    except Exception as exc:
        logger.exception("could not save decision on change %s", change_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "save_failed",
                "message": "We couldn't record that just now. Nothing has changed.",
            },
        ) from exc

    payload = record.as_dict()
    # The agents the client should now start, and only when there was a yes.
    payload["run_agents"] = list(record.proposal.get("agents") or []) if body.approved else []
    return payload


__all__ = ["router"]
