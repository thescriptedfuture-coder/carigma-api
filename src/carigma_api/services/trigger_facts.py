"""Build a `Facts` for one user, from the tables that actually hold them.

`services/triggers` decides what earns an email. This is what tells it what is
true. Without it the triggers are the referral service before its routes: real
logic with nothing feeding it, which looks finished and fires for nobody.

## Every source degrades to silence, never to a fact

Each read is wrapped. A table that is down contributes `None`, so the worst
case is an email that does not mention something — never an email that
announces something we could not actually see. The same rule Today's signal
loader follows, and for the same reason: a fabricated fact in an inbox cannot
be taken back.

## The windows are named, not scattered

`FOLLOW_UP_DAYS` and `RECENT` are constants because they are product decisions.
A `timedelta(days=7)` inline in a query is a decision nobody reviewed.

## Read-only

Nothing here writes. The send path claims the slot and the log records the
outcome; a producer that also marked things as seen would make "what would we
send" a destructive question, and the dry run would stop being repeatable.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from carigma_api.services.triggers import STRONG_MATCH, Facts

logger = logging.getLogger(__name__)

#: How long an application sits before the silence is worth naming. Long
#: enough that a normal process has had its chance; short enough that a
#: follow-up is still natural rather than archaeology.
FOLLOW_UP_DAYS = 10

#: How far back a "since you last looked" fact may reach. Beyond this it is
#: not news, and announcing week-old things is how a feature earns its
#: unsubscribe.
RECENT = timedelta(days=2)

#: Tracker stages that are still live. A closed or offered application is not
#: waiting on a follow-up.
OPEN_STAGES = ("applied", "screening", "interviewing")

#: Bands, low to high. A crossing is a change of index, which is what makes it
#: worth an email — three points inside a band is not news.
BANDS: tuple[tuple[int, str], ...] = (
    (0, "INVISIBLE"),
    (40, "EMERGING"),
    (60, "CLEAR"),
    (80, "MAGNETIC"),
)


def band_for(score: int) -> str:
    name = BANDS[0][1]
    for floor, label in BANDS:
        if score >= floor:
            name = label
    return name


def _safe(what: str, user_id: str, fn: Any) -> Any:
    """Run a source, or contribute nothing.

    Returns None on any failure. This is the whole degradation policy, in one
    place so no individual source can decide to be cleverer than it.
    """
    try:
        return fn()
    except Exception:
        logger.exception("trigger facts: %s failed for %s", what, user_id)
        return None


def top_match(client: Any, user_id: str, *, now: datetime) -> dict[str, Any] | None:
    """The best genuinely-new match, if it clears the bar.

    `first_seen` rather than `last_seen`: a job re-confirmed by today's scan is
    not new, and announcing it would make the same posting news twice.
    """
    res = (
        client.table("jobs_feed")
        .select("id,title,company,match_score,why_match,first_seen")
        .eq("user_id", user_id)
        .eq("status", "new")
        .order("match_score", desc=True)
        .limit(1)
        .execute()
    )
    rows = res.data if res and isinstance(res.data, list) else []
    if not rows:
        return None
    row = rows[0]
    seen = _parse(row.get("first_seen"))
    if seen is None or now - seen > RECENT:
        return None
    # falsy-ok: an unscored row is not a strong match
    if int(row.get("match_score") or 0) < STRONG_MATCH:
        return None
    return {
        "id": row.get("id"),
        "title": row.get("title"),
        "company": row.get("company"),
        "score": row.get("match_score"),
        "why": row.get("why_match"),
    }


def band_move(client: Any, user_id: str) -> dict[str, Any] | None:
    """A band CROSSING between the two most recent scores.

    Not a score change — a band change. Moving 61 to 64 is inside CLEAR and is
    not something to interrupt anyone about.

    The receipt is derived from the two scores themselves, so the email can
    never make a claim without one. `triggers._band_applies` requires it.
    """
    res = (
        client.table("score_history")
        .select("score,created_at")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .limit(2)
        .execute()
    )
    rows = res.data if res and isinstance(res.data, list) else []
    if len(rows) < 2:
        return None

    newest, previous = int(rows[0]["score"]), int(rows[1]["score"])
    to_band, from_band = band_for(newest), band_for(previous)
    if to_band == from_band:
        return None
    return {
        "from": from_band,
        "to": to_band,
        "improved": newest > previous,
        "receipt": f"your score went from {previous} to {newest}",
    }


def stale_application(client: Any, user_id: str, *, now: datetime) -> dict[str, Any] | None:
    """The oldest open application past the follow-up window.

    The OLDEST rather than the newest: if two have gone quiet, the one that has
    waited longest is the one worth naming.
    """
    res = (
        client.table("tracker")
        .select("id,company,title,stage,applied_at,updated_at")
        .eq("user_id", user_id)
        .order("applied_at", desc=False)
        .execute()
    )
    rows = res.data if res and isinstance(res.data, list) else []
    cutoff = now - timedelta(days=FOLLOW_UP_DAYS)

    for row in rows:
        if str(row.get("stage") or "") not in OPEN_STAGES:
            continue
        applied = _parse(row.get("applied_at"))
        if applied is None or applied > cutoff:
            continue
        # Touched recently means they are on it, and a nudge would be noise.
        touched = _parse(row.get("updated_at"))
        if touched is not None and touched > cutoff:
            continue
        return {
            "id": row.get("id"),
            "company": row.get("company"),
            "role": row.get("title"),
            "days": (now - applied).days,
        }
    return None


def profile_update(client: Any, user_id: str) -> dict[str, Any] | None:
    """Proposed changes waiting on their approval.

    `state = proposed` only. An applied update is finished, and a declined one
    is a decision — emailing about either would be asking a question they have
    already answered.
    """
    res = (
        client.table("profile_updates")
        .select("id,state")
        .eq("user_id", user_id)
        .eq("state", "proposed")
        .execute()
    )
    rows = res.data if res and isinstance(res.data, list) else []
    if not rows:
        return None
    return {"id": rows[0].get("id"), "changes": len(rows)}


def milestone(client: Any, user_id: str) -> dict[str, str] | None:
    """A round number of published posts, the first time it is reached.

    Deliberately narrow. A milestone the user would not recognise as an
    achievement is a manufactured reason to email, and this feature's whole
    constraint is that it must not manufacture reasons.
    """
    res = (
        client.table("content_loop")
        .select("id,published_at")
        .eq("user_id", user_id)
        .eq("state", "published")
        .execute()
    )
    rows = res.data if res and isinstance(res.data, list) else []
    count = len(rows)
    if count not in (10, 25, 50, 100):
        return None
    return {
        # Identity is the COUNT, so the tenth post is announced once and never
        # again — the eleventh does not re-fire it, and neither does a slot
        # being republished.
        "id": f"posts-{count}",
        "title": f"{count} posts shipped",
        "text": f"That is {count} posts published. Most people never get past three.",
    }


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def facts_for(
    client: Any, user_id: str, *, lapsed: bool = False, now: datetime | None = None
) -> Facts:
    """Everything true about this user, from the tables that hold it.

    A lapsed user short-circuits: the re-engagement sequence owns their inbox,
    and running five queries to build facts that `choose()` will discard is
    work for nothing.
    """
    if lapsed:
        return Facts(lapsed=True)

    at = now or datetime.now(UTC)
    return Facts(
        top_match=_safe("top_match", user_id, lambda: top_match(client, user_id, now=at)),
        band_move=_safe("band_move", user_id, lambda: band_move(client, user_id)),
        stale_application=_safe(
            "stale_application", user_id, lambda: stale_application(client, user_id, now=at)
        ),
        milestone=_safe("milestone", user_id, lambda: milestone(client, user_id)),
        profile_update=_safe("profile_update", user_id, lambda: profile_update(client, user_id)),
        lapsed=False,
    )


__all__ = [
    "BANDS",
    "FOLLOW_UP_DAYS",
    "OPEN_STAGES",
    "RECENT",
    "band_for",
    "band_move",
    "facts_for",
    "milestone",
    "profile_update",
    "stale_application",
    "top_match",
]
