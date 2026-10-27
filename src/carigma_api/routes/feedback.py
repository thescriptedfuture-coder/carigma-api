"""The feedback widget. Spec 9.3; V1's sidebar 💬, ported. Roadmap S15.

    POST /feedback    free — an idea, a bug, or a page rating

For a beta this is the whole input channel, and V2 had none: V1's sidebar
carried "💬 Feedback" with three kinds, and after the switch the people moving
over would have had no way to say what broke.

## Stored as V1 stored it

V1's `feedback` table and V1's encoding — the kind as a `[tag]` on the
message and a `|tag` on the page, the rating 1–5 only for a rating — so the
admin list (`GET /admin/feedback`) reads one history, not two.

## Written blind

`feedback` is write-only by design: an INSERT policy, no SELECT policy, so a
signed-in user can never read anyone's notes, their own included. Postgres
checks an `INSERT … RETURNING` against SELECT as well, and refuses it with
42501. V1 asked for the row back on every attempt that carried a user id, so
none of the four notes it ever stored says who sent it: only its anonymous
last resort got through (measured live, 27 Oct). This inserts with
`returning="minimal"` — nothing comes back, and an error is the only failure.

The user id comes from the verified token, never the body: the INSERT policy
is `with check (true)`, so the database would store whatever id it was handed.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from postgrest.types import ReturnMethod
from pydantic import BaseModel, Field, field_validator

from carigma_api.auth.dependencies import CurrentUser
from carigma_api.config import Settings, get_settings
from carigma_api.routes._guards import enforce_public_rate_limit
from carigma_api.services.repository import user_client

logger = logging.getLogger(__name__)

router = APIRouter(tags=["feedback"])

TABLE = "feedback"


class FeedbackRequest(BaseModel):
    #: V1's three: 💡 Idea, 🐞 Bug, ⭐ Rate page.
    kind: Literal["idea", "bug", "rating"]
    message: str = Field(max_length=2000)
    #: 1–5, and only with a rating — V1 offered stars only there.
    rating: int | None = Field(default=None, ge=1, le=5)
    #: Where they were, captured by the page rather than typed: V1 did the same.
    page: str = Field(default="", max_length=200)

    @field_validator("message")
    @classmethod
    def _said_something(cls, value: str) -> str:
        text = value.strip()
        if not text:
            # V1: "Please write a quick note first."
            raise ValueError("Write a quick note first.")
        return text


@router.post("/feedback", status_code=status.HTTP_201_CREATED)
def send_feedback(
    body: FeedbackRequest,
    request: Request,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """Store one note. Answers only whether it was stored."""
    # Per IP, 20 a minute: a note is typed by a person, and a loop is not one.
    enforce_public_rate_limit(request)

    token = (request.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
    row = {
        "user_id": user.id,
        "email": user.email or "",
        "message": f"[{body.kind}] {body.message}",
        "rating": body.rating if body.kind == "rating" else None,
        "page": f"{body.page.strip()}|{body.kind}",
    }
    try:
        user_client(settings, token).table(TABLE).insert(
            row, returning=ReturnMethod.minimal
        ).execute()
    except Exception as exc:
        logger.exception("FEEDBACK NOT STORED for %s — a note was lost unless they retry", user.id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "not_sent",
                "message": (
                    "We couldn't send that just now. Your note is still here, so try again "
                    "in a moment, or email it to support@carigma.in."
                ),
            },
        ) from exc
    return {"received": True}
