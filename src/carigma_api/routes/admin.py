"""Admin endpoints — contract §19.

Every route is behind `AdminUser` (the `ADMIN_EMAILS` allow-list, empty by
default so nobody is admin unless named). Reads use the **service key**, not
the caller's token: admin exists precisely to see across users, which per-user
RLS is designed to prevent.

That makes the allow-list the only thing standing between a normal account and
everyone's data, so it is checked on every route rather than at a router level
where a future `include_router` could bypass it.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from carigma_api.auth.dependencies import AdminUser, CurrentUser
from carigma_api.config import Settings, get_settings
from carigma_api.services.admin import (
    Adjustment,
    AdjustmentRefused,
    LowCreditUser,
    Overview,
    activation_rate,
    apply_adjustment,
    low_credit_summary,
)
from carigma_api.services.repository import emails_by_user_id, service_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


class AdjustRequest(BaseModel):
    delta: int
    #: Required, and the service layer refuses anything trivial. An adjustment
    #: with no reason is unauditable the moment the conversation is forgotten.
    reason: str = Field(min_length=3, max_length=280)


class NoteRequest(BaseModel):
    note: str = Field(min_length=1, max_length=2000)


class CreditRequestBody(BaseModel):
    """A user asking for more credits — the willingness-to-pay signal."""

    note: str | None = Field(default=None, max_length=1000)
    channel: str = Field(default="in_app")


def _db(settings: Settings) -> Any:
    """Service-role client. Admin reads across users by definition."""
    return service_client(settings)


# ── Overview ───────────────────────────────────────────────────────────────


@router.get("/overview")
def get_overview(
    admin: AdminUser, settings: Annotated[Settings, Depends(get_settings)]
) -> dict[str, Any]:
    """What greets the founder. The low-credit block is FIRST on purpose.

    A blocked user who cannot act and does not complain churns silently. This
    is the surface that prevents that, so it does not wait to be searched for.
    """
    db = _db(settings)
    now = datetime.now(UTC)

    users = _rows(db, "credits", "user_id,balance")
    emails = emails_by_user_id(db)
    open_requests = {
        r["user_id"]
        for r in _rows(db, "credit_requests", "user_id,resolved_at")
        if not r.get("resolved_at")
    }

    low = [
        LowCreditUser(
            user_id=str(r["user_id"]),
            email=emails.get(str(r["user_id"]), "—"),
            balance=int(
                r.get("balance") or 0
            ),  # falsy-ok: admin totals: a user with no credits row has contributed zero
            has_open_request=r["user_id"] in open_requests,
        )
        for r in users
    ]

    runs = _rows(db, "agent_runs", "status,started_at")
    recent_runs = [r for r in runs if _within(r.get("started_at"), now, days=7)]
    ledger = _rows(db, "credit_ledger", "delta,kind")

    overview = Overview(
        total_users=len(users),
        active_7d=len(
            {
                r.get("user_id")
                for r in _rows(db, "agent_runs", "user_id,started_at")
                if _within(r.get("started_at"), now, days=7)
            }
        ),
        active_30d=len(
            {
                r.get("user_id")
                for r in _rows(db, "agent_runs", "user_id,started_at")
                if _within(r.get("started_at"), now, days=30)
            }
        ),
        runs_7d=len(recent_runs),
        runs_failed_7d=sum(1 for r in recent_runs if r.get("status") == "failed"),
        credits_issued=sum(
            # falsy-ok: an absent ledger delta contributes nothing to a total
            int(r.get("delta") or 0)
            for r in ledger
            if int(r.get("delta") or 0)
            > 0  # falsy-ok: admin totals: a user with no credits row has contributed zero
        ),
        credits_consumed=abs(
            sum(
                int(r.get("delta") or 0) for r in ledger if int(r.get("delta") or 0) < 0
            )  # falsy-ok: admin totals: a user with no credits row has contributed zero
        ),
    )

    return {
        # First key, and the client renders it first. The ordering is the
        # feature.
        "low_credit": low_credit_summary(low),
        "overview": overview.as_dict(),
        "payments": {
            "enabled": settings.payments_live,
            "test_mode": settings.razorpay_is_test_mode,
        },
    }


# ── Users ──────────────────────────────────────────────────────────────────


@router.get("/users")
def list_users(
    admin: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
    q: str = Query(default=""),
    low_credit: bool = Query(default=False),
    limit: int = Query(default=50, le=200),
) -> dict[str, Any]:
    db = _db(settings)
    balances = {
        # falsy-ok: a user with no credits row has a balance of zero to spend
        r["user_id"]: int(r.get("balance") or 0)
        for r in _rows(
            db, "credits", "user_id,balance"
        )  # falsy-ok: admin totals: a user with no credits row has contributed zero
    }
    profiles = _rows(db, "profiles", "user_id,name,platforms,created_at")
    emails = emails_by_user_id(db)

    rows = [
        {
            "user_id": p["user_id"],
            "email": emails.get(str(p["user_id"]), ""),
            "name": p.get("name", ""),
            "balance": balances.get(str(p["user_id"]), 0),
            "platforms": p.get("platforms") or [],
            "created_at": p.get("created_at"),
        }
        for p in profiles
    ]

    if q:
        needle = q.lower()
        rows = [
            r for r in rows if needle in str(r["email"]).lower() or needle in str(r["name"]).lower()
        ]
    if low_credit:
        from carigma_api.services.admin import NEARLY_OUT_BELOW

        rows = [
            r for r in rows if int(r["balance"] or 0) < NEARLY_OUT_BELOW
        ]  # falsy-ok: admin totals: a user with no credits row has contributed zero

    rows.sort(key=lambda r: r["balance"])
    return {"items": rows[:limit], "total": len(rows)}


@router.get("/users/{user_id}")
def get_user(
    user_id: str, admin: AdminUser, settings: Annotated[Settings, Depends(get_settings)]
) -> dict[str, Any]:
    db = _db(settings)
    profile = _one(db, "profiles", "user_id", user_id)
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "no_user", "message": "No such user."},
        )
    balance = _one(db, "credits", "user_id", user_id) or {}

    return {
        "profile": profile,
        "balance": int(
            balance.get("balance") or 0
        ),  # falsy-ok: admin totals: a user with no credits row has contributed zero
        "ledger": _rows_for(db, "credit_ledger", user_id, limit=50),
        "runs": _rows_for(db, "agent_runs", user_id, limit=50),
        "scores": _rows_for(db, "score_history", user_id, limit=50),
        "notes": _rows_for(db, "admin_notes", user_id, limit=50),
        "credit_requests": _rows_for(db, "credit_requests", user_id, limit=20),
    }


@router.post("/users/{user_id}/credits")
def adjust_credits(
    user_id: str,
    body: AdjustRequest,
    admin: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """Manual top-up or correction. The operational heart of the beta.

    Writes to the ledger as `admin_adjustment` — deliberately distinct from
    purchase and signup, so "credits issued" can later be read honestly.
    """
    db = _db(settings)
    try:
        adjustment = Adjustment(
            user_id=user_id, delta=body.delta, reason=body.reason, author=admin.email
        )
    except AdjustmentRefused as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "adjustment_refused", "message": str(exc)},
        ) from exc

    current = int(
        (_one(db, "credits", "user_id", user_id) or {}).get("balance") or 0
    )  # falsy-ok: admin totals: a user with no credits row has contributed zero
    new_balance = apply_adjustment(current, adjustment)

    db.table("credits").upsert(
        {"user_id": user_id, "balance": new_balance, "updated_at": datetime.now(UTC).isoformat()}
    ).execute()
    db.table("credit_ledger").insert(adjustment.as_ledger_row(balance_after=new_balance)).execute()

    logger.info("admin %s adjusted %s by %s", admin.email, user_id, body.delta)
    return {
        "balance": new_balance,
        "delta": body.delta,
        "clamped": new_balance != current + body.delta,
    }


@router.post("/users/{user_id}/notes")
def add_note(
    user_id: str,
    body: NoteRequest,
    admin: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    _db(settings).table("admin_notes").insert(
        {"user_id": user_id, "note": body.note, "author": admin.email}
    ).execute()
    return {"saved": True}


# ── The willingness-to-pay signal ──────────────────────────────────────────


@router.get("/credit-requests")
def list_credit_requests(
    admin: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
    open_only: bool = Query(default=True),
) -> dict[str, Any]:
    """Who asked for more credits — the most valuable data in the beta.

    These are the people who would pay. Surfaced as a list with its own route
    rather than buried in a user detail view, because the pattern across users
    is the signal, not any single ask.
    """
    rows = _rows(db := _db(settings), "credit_requests", "*")
    emails = emails_by_user_id(db)
    if open_only:
        rows = [r for r in rows if not r.get("resolved_at")]
    rows.sort(key=lambda r: str(r.get("asked_at") or ""), reverse=True)

    return {
        "items": [{**r, "email": emails.get(str(r["user_id"]), "—")} for r in rows],
        "open_count": sum(1 for r in rows if not r.get("resolved_at")),
    }


@router.post("/credit-requests/{request_id}/resolve")
def resolve_credit_request(
    request_id: int,
    admin: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
    credits_granted: int = Query(default=0),
) -> dict[str, Any]:
    _db(settings).table("credit_requests").update(
        {
            "resolved_at": datetime.now(UTC).isoformat(),
            "resolved_by": admin.email,
            "credits_granted": credits_granted,
        }
    ).eq("id", request_id).execute()
    return {"resolved": True}


# ── Feedback and testimonials ──────────────────────────────────────────────


@router.get("/feedback")
def list_feedback(
    admin: AdminUser, settings: Annotated[Settings, Depends(get_settings)]
) -> dict[str, Any]:
    """Every feedback submission and every "what did Carigma change for you?"
    answer. Social proof — surfaced, not buried."""
    db = _db(settings)
    rows = _rows(db, "feedback", "*")
    emails = emails_by_user_id(db)
    rows.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)

    return {
        "items": [{**r, "email": emails.get(str(r.get("user_id")), "—")} for r in rows],
        "total": len(rows),
    }


# ── Agent runs log ─────────────────────────────────────────────────────────


@router.get("/runs")
def list_runs(
    admin: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
    limit: int = Query(default=100, le=500),
) -> dict[str, Any]:
    """Where the never-charge-on-failure invariant is VISIBLY true.

    `charged` sits next to `status` in every row, so a failed run showing a
    non-zero charge is immediately obvious to the eye — no query required.
    """
    db = _db(settings)
    rows = _rows(db, "agent_runs", "*")
    rows.sort(key=lambda r: str(r.get("started_at") or ""), reverse=True)
    rows = rows[:limit]

    items = []
    for r in rows:
        started, finished = _parse(r.get("started_at")), _parse(r.get("finished_at"))
        items.append(
            {
                "id": r.get("id"),
                "user_id": r.get("user_id"),
                "agent": r.get("agent"),
                "status": r.get("status"),
                "charged": int(
                    r.get("credits_charged") or 0
                ),  # falsy-ok: admin totals: a user with no credits row has contributed zero
                "duration_ms": (
                    int((finished - started).total_seconds() * 1000)
                    if started and finished
                    else None
                ),
                "error": r.get("error"),
                "started_at": r.get("started_at"),
            }
        )

    # Computed here rather than left to the eye alone: if this is ever
    # non-empty, the credit rule has been broken and admin says so loudly.
    violations = [
        i
        for i in items
        if i["status"] in ("failed", "cancelled", "empty")
        and int(i["charged"] or 0)
        > 0  # falsy-ok: admin totals: a user with no credits row has contributed zero
    ]
    return {
        "items": items,
        "charge_rule_violations": violations,
        "charge_rule_ok": not violations,
    }


# ── System ─────────────────────────────────────────────────────────────────


@router.get("/system")
def get_system(
    admin: AdminUser, settings: Annotated[Settings, Depends(get_settings)]
) -> dict[str, Any]:
    """Health, flags, and a MASKED env check.

    Masked, not hidden: "is ANTHROPIC_API_KEY set?" is the operational question,
    and printing the value to answer it would put a secret in a browser tab and
    everyone's screen-share.
    """
    return {
        "flags": {
            "payments_enabled": settings.payments_enabled,
            "payments_live": settings.payments_live,
            "razorpay_test_mode": settings.razorpay_is_test_mode,
        },
        "env": {
            name: _mask(value)
            for name, value in (
                ("SUPABASE_URL", settings.supabase_url),
                ("SUPABASE_SERVICE_KEY", settings.supabase_service_key),
                ("ANTHROPIC_API_KEY", settings.anthropic_api_key),
                ("JSEARCH_API_KEY", settings.jsearch_api_key),
                ("ADZUNA_APP_ID", settings.adzuna_app_id),
                ("RAZORPAY_KEY_ID", settings.razorpay_key_id),
                ("RAZORPAY_KEY_SECRET", settings.razorpay_key_secret),
            )
        },
        "limits": {
            "jsearch_daily_cap": settings.jsearch_daily_cap,
            "jobs_cache_ttl_hours": settings.jobs_cache_ttl_hours,
        },
    }


def _mask(value: str) -> dict[str, Any]:
    """Set-or-not, plus a short fingerprint. Never the value.

    The last four characters are enough to tell two keys apart when checking
    which one is deployed, and not enough to be a key.
    """
    if not value:
        return {"set": False, "hint": None}
    return {"set": True, "hint": f"…{value[-4:]}" if len(value) > 8 else "…"}


# ── Analytics ──────────────────────────────────────────────────────────────


@router.get("/analytics")
def get_analytics(
    admin: AdminUser, settings: Annotated[Settings, Depends(get_settings)]
) -> dict[str, Any]:
    db = _db(settings)
    profiles = _rows(db, "profiles", "user_id,created_at")
    scored = {r.get("user_id") for r in _rows(db, "score_history", "user_id")}
    runs = _rows(db, "agent_runs", "user_id,agent,started_at")

    # Which agent someone runs FIRST — where the product earns its first yes.
    first_agent: dict[str, str] = {}
    for r in sorted(runs, key=lambda x: str(x.get("started_at") or "")):
        first_agent.setdefault(str(r.get("user_id")), str(r.get("agent")))
    counts: dict[str, int] = {}
    for agent in first_agent.values():
        counts[agent] = counts.get(agent, 0) + 1

    signups = [d for p in profiles if (d := _parse(p.get("created_at")))]
    return {
        "signups_total": len(profiles),
        "signups_by_week": _by_week(signups),
        "activation": activation_rate(len(profiles), len(scored)),
        "first_agent": counts,
    }


def _by_week(dates: list[datetime]) -> list[dict[str, Any]]:
    buckets: dict[str, int] = {}
    for d in dates:
        key = f"{d.isocalendar().year}-W{d.isocalendar().week:02d}"
        buckets[key] = buckets.get(key, 0) + 1
    return [{"week": k, "count": v} for k, v in sorted(buckets.items())]


# ── User-facing: capture the ask ───────────────────────────────────────────

request_router = APIRouter(tags=["credits"])


@request_router.post("/credits/request")
def request_credits(
    body: CreditRequestBody,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """The user asking for more credits.

    Free, obviously, and deliberately in-product rather than a mailto: link —
    a WhatsApp message is a signal that dies in a phone. This is the highest
    value datum of the beta and it needs somewhere to live.
    """
    _db(settings).table("credit_requests").insert(
        {"user_id": user.id, "note": body.note, "channel": "in_app"}
    ).execute()

    return {
        "received": True,
        # A real commitment with a real timeframe, not "we'll be in touch".
        "message": "Got it. We top up by hand during beta — usually within a few hours.",
    }


# ── Small helpers ──────────────────────────────────────────────────────────


def _rows(db: Any, table: str, columns: str) -> list[dict[str, Any]]:
    try:
        return list(db.table(table).select(columns).execute().data or [])
    except Exception:
        # One unreadable table must not blank the whole admin page. Logged so
        # the gap is visible rather than looking like "no data".
        logger.exception("admin read failed for %s", table)
        return []


def _rows_for(db: Any, table: str, user_id: str, *, limit: int) -> list[dict[str, Any]]:
    try:
        return list(
            db.table(table).select("*").eq("user_id", user_id).limit(limit).execute().data or []
        )
    except Exception:
        logger.exception("admin read failed for %s/%s", table, user_id)
        return []


def _one(db: Any, table: str, column: str, value: str) -> dict[str, Any] | None:
    rows = _rows_by(db, table, column, value)
    return rows[0] if rows else None


def _rows_by(db: Any, table: str, column: str, value: str) -> list[dict[str, Any]]:
    try:
        return list(db.table(table).select("*").eq(column, value).limit(1).execute().data or [])
    except Exception:
        logger.exception("admin read failed for %s.%s", table, column)
        return []


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _within(value: Any, now: datetime, *, days: int) -> bool:
    parsed = _parse(value)
    return bool(parsed and parsed >= now - timedelta(days=days))


__all__ = ["router", "request_router"]
