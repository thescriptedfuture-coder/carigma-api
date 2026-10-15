"""The shared JSearch quota: a cache in front of it, and a hard daily cap.

## Declared, and never built

`config.py` says it plainly:

    # 3. The JSearch quota is a SHARED resource handled by the jobs cache +
    #    daily cap, NOT by a per-user limiter. Do not duplicate it here.

`jsearch_daily_cap` was read by exactly one caller — the admin overview, which
*displays* it — and `jobs_cache_ttl_hours` by nothing at all. So the mechanism
that was supposed to stop V2 exhausting the quota out from under live V1 users
existed as two numbers and a comment.

That is the orphan class applied to a safety limit, which is the worst place
for it: a cap nobody enforces reads in every file as though it is enforced.

## Cannot count is not counted zero

`fetches_today` returns `int | None`, and `None` REFUSES.

`0` grants permission to call a paid API. If the count cannot be read we do not
know whether 0 or 25 calls have been made today, and the recoverable direction
is to skip a scan — a user waits; the alternative spends a shared resource that
belongs to people already paying for the product.

## The cache is the first line, not an optimisation

A query answered from cache costs no quota at all. That is why the TTL is 48
hours rather than minutes: job postings are perishable over days, not seconds,
and V1 chose the same number.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

CACHE = "jobs_cache"

#: The provider whose quota is shared with live V1 users. Adzuna has its own
#: free tier and is not what the cap exists to protect.
CAPPED_SOURCE = "jsearch"


def query_key(*, role: str, city: str, source: str) -> str:
    """One stable key per (role, city, source).

    Hashed rather than concatenated because the column is unique and a role
    title can carry anything — `Business Analyst — ERP & Public Finance / MoD`
    is a real one. A readable prefix is kept so a human reading the table can
    tell what it was.
    """
    normalised = f"{source}|{role.strip().lower()}|{city.strip().lower()}"
    digest = hashlib.sha256(normalised.encode()).hexdigest()[:16]
    return f"{source}:{role.strip().lower()[:40]}:{digest}"


def fetches_today(
    db: Any, *, source: str = CAPPED_SOURCE, now: datetime | None = None
) -> int | None:
    """How many provider calls today, or `None` when we cannot tell.

    **`None` is not zero.** Zero grants permission to spend a shared quota;
    not knowing must not.
    """
    moment = now or datetime.now(UTC)
    midnight = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        res = (
            db.table(CACHE)
            .select("id")
            .eq("source", source)
            .gte("fetched_at", midnight.isoformat())
            .execute()
        )
    except Exception:
        logger.exception("could not count today's %s fetches", source)
        return None
    rows = res.data if res and isinstance(res.data, list) else None
    return len(rows) if rows is not None else None


def refusal(
    db: Any, *, cap: int, source: str = CAPPED_SOURCE, now: datetime | None = None
) -> str | None:
    """Why this scan must not call the provider, or `None` to go ahead.

    A sentence rather than a bool, because the caller has to tell somebody.
    """
    used = fetches_today(db, source=source, now=now)
    if used is None:
        return (
            "We couldn't check today's search quota, so we didn't spend any of it. "
            "Nothing was charged."
        )
    if used >= cap:
        return (
            f"Today's search quota is used up ({used} of {cap}). "
            "It resets at midnight UTC. Nothing was charged."
        )
    return None


def cached(
    db: Any, *, key: str, ttl_hours: int, now: datetime | None = None
) -> list[dict[str, Any]] | None:
    """A fresh cached payload, or `None`.

    `None` covers three different things on purpose — no row, a stale row, and
    a read that failed — because every one of them means the same to the
    caller: there is nothing here, go and fetch. The distinction that matters
    is the one in `fetches_today`, where the two answers differ.
    """
    moment = now or datetime.now(UTC)
    try:
        res = db.table(CACHE).select("payload,fetched_at").eq("query_key", key).execute()
    except Exception:
        logger.exception("could not read the jobs cache for %s", key)
        return None

    rows = res.data if res and isinstance(res.data, list) else []
    if not rows:
        return None

    row = rows[0]
    fetched = row.get("fetched_at")
    try:
        when = datetime.fromisoformat(str(fetched).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        logger.error("unparseable fetched_at %r in the jobs cache", fetched)
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    if moment - when > timedelta(hours=ttl_hours):
        return None

    payload = row.get("payload")
    return payload if isinstance(payload, list) else None


def remember(db: Any, *, key: str, source: str, payload: list[dict[str, Any]]) -> None:
    """Record what a provider returned, so the next identical query is free.

    Upserts on `query_key`, which is the table's unique key — named explicitly,
    because an upsert resolves on the PRIMARY key when no target is given and
    this table's is `id`. That mistake cost a week on `profiles`.

    Never raises: the jobs were fetched and are about to be shown. Failing the
    scan because the cache write failed would throw away work already paid for.
    """
    try:
        db.table(CACHE).upsert(
            {
                "query_key": key,
                "source": source,
                "payload": payload,
                "fetched_at": datetime.now(UTC).isoformat(),
            },
            on_conflict="query_key",
        ).execute()
    except Exception:
        logger.exception("could not write the jobs cache for %s", key)


__all__ = [
    "CACHE",
    "CAPPED_SOURCE",
    "cached",
    "fetches_today",
    "query_key",
    "refusal",
    "remember",
]
