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
decision alone.

## `details` is one column, and an update replaces all of it

The first real scans saved thirteen postings and then wrote their scores with
`update({"details": {matched_skills, missing_skills, keyRequirements}})`. A
jsonb column is a single value: that update did not ADD three keys, it
REPLACED the whole object, and the apply link, the publisher, the source and
the posting date went with it. Every posting Career Scout found had no way to
apply by the time the user saw it.

So nothing here writes `details` without starting from what is stored, and the
merge is in one place — `annotate` — rather than at each call site.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from carigma_api.services.jobs import scoring
from carigma_api.services.jobs.models import NormalizedJob

logger = logging.getLogger(__name__)

TABLE = "jobs_feed"


def provider_facts(job: NormalizedJob) -> dict[str, Any]:
    """What the SOURCE says about a posting — the part of `details` a scan owns.

    Exactly the keys `present_job` reads back out. Written as a literal rather
    than `asdict`, because a field added to the model would otherwise appear in
    the payload with nothing rendering it.

    Everything else in `details` belongs to someone else: the scoring pass
    (`matched_skills`, `missing_skills`, `keyRequirements`) or V1 (`applyLinks`,
    `cvTweaks`, ...). A scan must not touch those.
    """
    return {
        "apply_url": job.apply_url,
        "apply_options": [
            {"publisher": o.publisher, "url": o.url, "is_direct": o.is_direct}
            for o in job.apply_options
        ],
        "publisher": job.publisher,
        "source": job.source,
        "posted_at": job.posted_at.isoformat() if job.posted_at else None,
        "expires_at": job.expires_at.isoformat() if job.expires_at else None,
    }


def _refreshed(stored: dict[str, Any], job: NormalizedJob) -> dict[str, Any]:
    """Stored details with this sighting's facts laid over them.

    Only facts the source actually stated this time. A posting date the source
    left out today is not evidence that there is no posting date, and laying
    `None` over a stored one would erase a fact to record an absence.
    """
    out = dict(stored)
    for key, value in provider_facts(job).items():
        if value is None or value == []:
            continue
        out[key] = value
    return out


def _row(job: NormalizedJob, *, user_id: str, now: datetime) -> dict[str, Any]:
    """One normalised job as the table holds it, for a FIRST sighting.

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
        # The scoring pass adds its keys later. Absent, not empty: `[]` would
        # mean "compared, found nothing", which is a different row.
        "details": provider_facts(job),
        "last_seen": now.isoformat(),
    }


@dataclass
class Persisted:
    """What one scan did to the feed."""

    #: The rows this scan created: id, key, title, company. `id` is None only when
    #: the database did not hand the row back, which supabase-py always does.
    new: list[dict[str, Any]] = field(default_factory=list)
    seen_again: int = 0
    #: `dedup_key` -> the `details` now stored, so `annotate` merges into what
    #: is THERE rather than into nothing.
    details: dict[str, dict[str, Any]] = field(default_factory=dict)


def persist(
    db: Any, *, user_id: str, jobs: list[NormalizedJob], now: datetime | None = None
) -> Persisted:
    """Store a scan's results.

    Two statements rather than one upsert, because the two cases genuinely
    differ: an existing row must keep its `first_seen`, its `status`, its
    `apply_kit` and every key of `details` the scan does not own, and an upsert
    of the whole row would flatten all of them.
    """
    moment = now or datetime.now(UTC)
    if not jobs:
        return Persisted()

    keys = [job.dedupe_key for job in jobs]
    try:
        res = (
            db.table(TABLE)
            .select("id,dedup_key,details")
            .eq("user_id", user_id)
            .in_("dedup_key", keys)
            .execute()
        )
        known: dict[str, dict[str, Any]] = {
            r["dedup_key"]: r for r in (res.data or []) if r.get("dedup_key")
        }
    except Exception:
        logger.exception("could not read existing feed rows for %s", user_id)
        # Refusing to write at all is the recoverable direction: inserting
        # blind would duplicate every row the user already has.
        raise

    out = Persisted()
    fresh = [job for job in jobs if job.dedupe_key not in known]
    again = [job for job in jobs if job.dedupe_key in known]

    if fresh:
        rows = [
            {
                **_row(job, user_id=user_id, now=moment),
                "first_seen": moment.isoformat(),
                "status": "active",
            }
            for job in fresh
        ]
        res = db.table(TABLE).insert(rows).execute()
        back = res.data if res is not None and isinstance(res.data, list) else []
        returned = {r.get("dedup_key"): r for r in back if isinstance(r, dict)}
        for row in rows:
            echoed = returned.get(row["dedup_key"]) or {}
            out.new.append(
                {
                    "id": echoed.get("id"),
                    "dedup_key": row["dedup_key"],
                    "title": row["title"],
                    "company": row["company"],
                }
            )
            out.details[row["dedup_key"]] = row["details"]

    for job in again:
        stored = known[job.dedupe_key].get("details")
        details = _refreshed(stored if isinstance(stored, dict) else {}, job)
        # `last_seen`, and the source's own facts merged over what is stored.
        # NOT `status` — a dismissed row stays dismissed — and not `first_seen`,
        # which is what "New today" is measured from.
        db.table(TABLE).update({"last_seen": moment.isoformat(), "details": details}).eq(
            "user_id", user_id
        ).eq("dedup_key", job.dedupe_key).execute()
        out.details[job.dedupe_key] = details

    out.seen_again = len(again)
    return out


def annotate(
    db: Any,
    *,
    user_id: str,
    jobs: list[NormalizedJob],
    annotations: dict[int, scoring.Annotation],
    stored: dict[str, dict[str, Any]],
) -> dict[str, int]:
    """Write the scores onto rows `persist` just wrote. Returns key -> score.

    Separate from `persist` on purpose: persisting is what makes a posting
    exist for this user and must not depend on the model; annotating is extra
    and is allowed to do nothing. A job nobody scored keeps `match_score` NULL,
    which the feed renders as "not analysed" — V1 showed 70%.

    `stored` is what `persist` left in `details`. The annotation is merged INTO
    it; the module docstring says what merging into `{}` did.
    """
    scores: dict[str, int] = {}
    for index, annotation in annotations.items():
        key = jobs[index].dedupe_key
        if key not in stored:
            # Not written by this scan, so what its `details` hold is unknown —
            # and an update from nothing is the bug this replaced.
            logger.error("refusing to annotate %s: its stored details are unknown", key)
            continue
        row = scoring.merge({"details": stored[key]}, annotation)
        try:
            db.table(TABLE).update(row).eq("user_id", user_id).eq("dedup_key", key).execute()
        except Exception:
            # The posting is saved and real. Losing its annotation is a worse
            # feed, not a failed scan, and the run has already been paid for.
            logger.exception("could not annotate %s", key)
            continue
        if annotation.match_score is not None:
            scores[key] = annotation.match_score
    return scores


__all__ = ["TABLE", "Persisted", "annotate", "persist", "provider_facts"]
