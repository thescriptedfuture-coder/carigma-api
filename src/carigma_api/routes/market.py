"""Market intelligence: the admin curation loop and the user-facing wire.

    GET   /admin/market            every batch, newest first (admin)
    POST  /admin/market            create or replace a DRAFT (admin)
    POST  /admin/market/{key}/publish   make it live (admin)
    GET   /market/wire             what a user sees — or an honest nothing

The publish step is the honesty mechanism. `assert_publishable` refuses an
unsourced or empty batch, so "real and sourced, never fabricated" is enforced
at the one door everything passes through rather than hoped for in a prompt.

The user-facing read goes through `market.wire_for`, which is the same gate the
Sunday digest uses. There is deliberately no way to ask this module for "the
most recent batch" — that fallback is the manufactured-contact failure mode,
and the only way to be sure it never happens is for it not to exist.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from carigma_api.auth.dependencies import AdminUser, CurrentUser
from carigma_api.config import Settings, get_settings
from carigma_api.services.market import (
    Batch,
    MarketItem,
    NotPublishable,
    assert_publishable,
    wire_for,
)
from carigma_api.services.reengagement import Sequence
from carigma_api.services.repository import (
    _as_rows,
    emails_by_user_id,
    service_client,
    user_client,
)

logger = logging.getLogger(__name__)

admin_router = APIRouter(prefix="/admin", tags=["admin"])
router = APIRouter(tags=["market"])

TABLE = "market_digests"


class ItemBody(BaseModel):
    headline: str = Field(min_length=1, max_length=200)
    detail: str = Field(default="", max_length=1000)
    source_name: str = Field(min_length=1, max_length=120)
    #: Not `HttpUrl`: the service checks the scheme and we want ITS rule to be
    #: the one that decides, so a future relaxation here cannot quietly let an
    #: uncheckable claim through.
    source_url: str = Field(min_length=1, max_length=500)


class BatchBody(BaseModel):
    batch_key: str = Field(min_length=3, max_length=64)
    items: list[ItemBody] = Field(default_factory=list)
    #: When this batch stops being live. Optional — the service falls back to
    #: DEFAULT_LIFETIME so a curator who forgets does not create an
    #: indefinitely fresh digest.
    next_refresh: datetime | None = None


def _rows(db: Any) -> list[dict[str, Any]]:
    """Narrowed through `_as_rows`: supabase-py types `.data` as a loose JSON
    union, and coercing it by hand is how a malformed response becomes a
    silent data bug."""
    res = db.table(TABLE).select("*").order("published_at", desc=True).limit(20).execute()
    return _as_rows(res.data if res else None)


# ── Admin: survey → curate → publish ───────────────────────────────────────


@admin_router.get("/market")
def list_batches(
    admin: AdminUser, settings: Annotated[Settings, Depends(get_settings)]
) -> dict[str, Any]:
    """Every batch with its state, so the curator can see what is live."""
    now = datetime.now(UTC)
    batches = [Batch.from_row(r) for r in _rows(service_client(settings))]
    return {
        "batches": [b.as_dict(now) for b in batches],
        # Stated rather than left to be inferred from the list: "is anything
        # live right now?" is the question this screen exists to answer.
        "live": next((b.batch_key for b in batches if b.is_live(now)), None),
    }


@admin_router.post("/market", status_code=status.HTTP_201_CREATED)
def upsert_batch(
    body: BatchBody, admin: AdminUser, settings: Annotated[Settings, Depends(get_settings)]
) -> dict[str, Any]:
    """Create or replace a DRAFT.

    Never publishes. Saving and going live are separate acts because the whole
    guarantee rests on a human choosing the second one deliberately.
    """
    db = service_client(settings)
    payload = {
        "batch_key": body.batch_key,
        "items": [i.model_dump() for i in body.items],
        "next_refresh": body.next_refresh.isoformat() if body.next_refresh else None,
        "created_by": admin.id,
        "updated_at": datetime.now(UTC).isoformat(),
    }
    try:
        db.table(TABLE).upsert(payload, on_conflict="batch_key").execute()
    except Exception as exc:
        logger.exception("could not save market batch %s", body.batch_key)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "save_failed", "message": "Couldn't save that batch."},
        ) from exc

    return {"batch_key": body.batch_key, "state": "draft", "items": len(body.items)}


@admin_router.post("/market/{batch_key}/publish")
def publish_batch(
    batch_key: str, admin: AdminUser, settings: Annotated[Settings, Depends(get_settings)]
) -> dict[str, Any]:
    """Make a batch live. The one door everything user-facing passes through."""
    db = service_client(settings)
    res = db.table(TABLE).select("*").eq("batch_key", batch_key).execute()
    rows = _as_rows(res.data if res else None)
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "not_found", "message": f"No batch {batch_key}."},
        )

    items = [MarketItem.from_stored(i) for i in (rows[0].get("items") or [])]
    try:
        assert_publishable(items)
    except NotPublishable as exc:
        # 422 rather than 400: the request is well formed, the CONTENT is not
        # publishable. The distinction matters to the admin surface, which
        # shows this message verbatim.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "not_publishable", "message": str(exc)},
        ) from exc

    now = datetime.now(UTC)
    try:
        db.table(TABLE).update({"published_at": now.isoformat(), "updated_at": now.isoformat()}).eq(
            "batch_key", batch_key
        ).execute()
    except Exception as exc:
        logger.exception("could not publish market batch %s", batch_key)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "publish_failed",
                "message": "Couldn't publish that. It is still a draft.",
            },
        ) from exc

    published = Batch.from_row({**rows[0], "published_at": now.isoformat()})
    return published.as_dict(now)


@admin_router.get("/reengagement")
def list_reengagement(
    admin: AdminUser, settings: Annotated[Settings, Depends(get_settings)]
) -> dict[str, Any]:
    """Lapse-and-return history.

    A free consequence of modelling one row per SEQUENCE rather than a counter
    on the user: every lapse episode is still here, with how many digests it
    took and whether the person came back. A counter would have overwritten all
    of it.

    `returned` is the number worth watching — it is the only evidence the
    programme does anything. `finished` is eight sends with no return, and a
    rising finished:returned ratio is the argument for changing the cadence or
    stopping.
    """
    db = service_client(settings)
    try:
        res = (
            db.table("reengagement_sequences")
            .select("*")
            .order("started_at", desc=True)
            .limit(100)
            .execute()
        )
        rows = _as_rows(res.data if res else None)
    except Exception:
        logger.exception("could not read reengagement sequences")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "read_failed", "message": "Couldn't load re-engagement history."},
        ) from None

    emails = emails_by_user_id(db)
    sequences = [Sequence.from_row(r) for r in rows]

    by_state = Counter(str(s.state) for s in sequences)
    returned = by_state.get("returned", 0)
    finished = by_state.get("finished", 0)

    return {
        "sequences": [
            {
                **s.as_dict(),
                "email": emails.get(s.user_id, ""),
                "started_at": r.get("started_at"),
            }
            for s, r in zip(sequences, rows, strict=True)
        ],
        "counts": {
            "active": by_state.get("active", 0),
            "returned": returned,
            "finished": finished,
        },
        # Stated, not left to be divided by eye. Zero returns out of any
        # meaningful number of finished sequences is the signal to stop.
        "headline": (
            f"{returned} came back · {finished} ran to the end"
            if (returned or finished)
            else "No completed sequences yet."
        ),
    }


# ── The user-facing wire ───────────────────────────────────────────────────


@router.get("/market/wire")
def get_wire(
    request: Request,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """What a signed-in user sees. Free — this is not an agent run.

    Read through the user client so RLS applies: `market_digests` is
    authenticated-and-published-only, which is a second, independent reason a
    draft cannot reach a user even if this code were wrong.
    """
    token = (request.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
    db = user_client(settings, token)
    try:
        res = db.table(TABLE).select("*").limit(20).execute()
        rows = _as_rows(res.data if res else None)
    except Exception:
        logger.exception("could not read the market wire")
        # Not "no research this week" — we could not look, and those are
        # different facts. ProviderUnavailable is never rendered as empty.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "read_failed", "message": "We couldn't load the market read."},
        ) from None

    return wire_for([Batch.from_row(r) for r in rows], datetime.now(UTC)).as_dict()


__all__ = ["admin_router", "router"]
