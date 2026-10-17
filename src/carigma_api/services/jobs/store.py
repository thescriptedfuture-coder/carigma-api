"""Writing a scan's results into `jobs_feed`, and deciding what is new.

## `first_seen` is the whole point

V1's rule, and the reason the table has two timestamps: a rescan UPDATEs
`last_seen` on a row it has seen before and INSERTs only genuinely new roles.
`first_seen` therefore survives rescans, and "New today" means the posting
arrived today rather than "the last scan ran today".

Get this wrong in the obvious direction — upsert everything with a fresh
`first_seen` — and every scan reports its whole result as new. The badge then
says 12 new every morning forever, which is the same lie as a counter that
cannot count.

## A dismissed job never comes back

`status = 'dismissed'` is a decision the user made about that posting. A later
scan finds it again, because the posting is still live, and must leave the
decision alone. Only `last_seen` moves.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from carigma_api.services.jobs.models import NormalizedJob

logger = logging.getLogger(__name__)

TABLE = "jobs_feed"


def _row(job: NormalizedJob, *, user_id: str, now: datetime) -> dict[str, Any]:
    """One normalised job as the table holds it.

    Written from the dataclass rather than from memory of it — the column list
    is in `schema_snapshot.json` and `test_write_shapes.py` checks every key
    here exists.
    """
    return {
        "user_id": user_id,
        # The MODEL spells it `dedupe_key`; the COLUMN is `dedup_key`. Both
        # names are right in their own place and neither is a typo, so the
        # translation happens here, once, at the boundary.
        "dedup_key": job.dedupe_key,
        "title": job.title,
        "company": job.company,
        "location": job.location,
        "job_type": job.job_type,
        "salary": job.salary,
        # Exactly the keys `present_job` reads back out. Written as a literal
        # rather than `asdict`, because a field added to the model would
        # otherwise appear in the payload with nothing rendering it — a field
        # nothing renders is a trap, and this is where one would be planted.
        "details": {
            "apply_url": job.apply_url,
            "apply_options": [
                {"publisher": o.publisher, "url": o.url, "is_direct": o.is_direct}
                for o in job.apply_options
            ],
            "publisher": job.publisher,
            "source": job.source,
            "posted_at": job.posted_at.isoformat() if job.posted_at else None,
            # The scoring pass fills these. Absent, not empty: `[]` would mean
            # "compared, found nothing", which is a different week of row.
        },
        "last_seen": now.isoformat(),
    }


def persist(
    db: Any, *, user_id: str, jobs: list[NormalizedJob], now: datetime | None = None
) -> tuple[int, int]:
    """Store a scan's results. Returns `(new, seen again)`.

    Two statements rather than one upsert, because the two cases genuinely
    differ: an existing row must keep its `first_seen`, its `status` and its
    `apply_kit`, and an upsert of the whole row would flatten all three.
    """
    moment = now or datetime.now(UTC)
    if not jobs:
        return 0, 0

    keys = [job.dedupe_key for job in jobs]
    try:
        res = (
            db.table(TABLE)
            .select("dedup_key")
            .eq("user_id", user_id)
            .in_("dedup_key", keys)
            .execute()
        )
        known = {r["dedup_key"] for r in (res.data or []) if r.get("dedup_key")}
    except Exception:
        logger.exception("could not read existing feed rows for %s", user_id)
        # Refusing to write at all is the recoverable direction: inserting
        # blind would duplicate every row the user already has.
        raise

    fresh = [job for job in jobs if job.dedupe_key not in known]
    again = [job for job in jobs if job.dedupe_key in known]

    if fresh:
        db.table(TABLE).insert(
            [
                {
                    **_row(job, user_id=user_id, now=moment),
                    "first_seen": moment.isoformat(),
                    "status": "active",
                }
                for job in fresh
            ]
        ).execute()

    for job in again:
        # ONLY `last_seen`. A row the user dismissed stays dismissed, a kit
        # already paid for stays attached, and `first_seen` is what "New today"
        # is measured from.
        db.table(TABLE).update({"last_seen": moment.isoformat()}).eq("user_id", user_id).eq(
            "dedup_key", job.dedupe_key
        ).execute()

    return len(fresh), len(again)


__all__ = ["TABLE", "persist"]
