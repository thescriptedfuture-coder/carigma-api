"""Gathering what Today knows, from four tables that are mostly empty.

## Measured before mapping

    tracker          0 rows   brand new
    content_loop     0 rows   brand new
    thread_decisions 0 rows   brand new
    score_history   37 rows   10 users
    jobs_feed       93 rows    6 users
    profiles        12 rows   `last_seen_jobs_at` set on 8

So the common beta state is PARTIAL: a score history and a job feed, no
tracker, no planned week. Every reader here has to work with that rather than
assume a fully populated user, and the distinctions the Posts port surfaced —
never-planned versus paused — are live on this path.

## "We do not know when you last looked" is not "everything is new"

Four of twelve profiles have no `last_seen_jobs_at`. Counting every active job
as new would greet someone with "15 new matches" on their first ever load,
which is a claim about a comparison nobody made. `new_match_count` is `None`
there, and the ladder says "15 matches waiting" instead — true either way, and
only one of them claims novelty.

## Reading is not stamping

`load_signals` does NOT update `last_seen_jobs_at`. Marking the feed as seen
because Today rendered a count would mean the number is only ever right once,
and would silently consume the "new" state of jobs the user has not opened.
The Jobs surface owns that stamp.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any

from carigma_api.services.jobs.feed import DEFAULT_STATUSES
from carigma_api.services.ladder import Signals
from carigma_api.services.posts import SlotState
from carigma_api.services.posts_store import SupabasePlanStore

logger = logging.getLogger(__name__)

TRACKER = "tracker"
JOBS = "jobs_feed"

#: Stages where an interview is still ahead of the user. A closed application
#: has no upcoming anything, however its `interview_at` reads.
OPEN_STAGES = ("applied", "screening", "interview")


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        logger.error("unparseable timestamp %r on a Today signal", value)
        return None


def _monday_of(value: date) -> date:
    return value - timedelta(days=value.weekday())


def next_interview(rows: list[dict[str, Any]], *, now: datetime) -> tuple[datetime | None, str]:
    """The soonest interview still ahead, and what to call it.

    Closed applications are excluded even when they carry an `interview_at` —
    an interview you already had, or for a role that is gone, is not something
    to prepare for.
    """
    upcoming: list[tuple[datetime, str]] = []
    for row in rows:
        if row.get("closed_at") or str(row.get("stage") or "") not in OPEN_STAGES:
            continue
        when = _parse(row.get("interview_at"))
        if when is None or when < now:
            continue
        label = ", ".join(p for p in (row.get("company"), row.get("title")) if p)
        upcoming.append((when, label))

    if not upcoming:
        return None, ""
    soonest = min(upcoming, key=lambda pair: pair[0])
    return soonest[0], soonest[1]


def post_due_today(plan: Any, *, today: date) -> str | None:
    """A slot due today that is neither published nor skipped.

    `None` when there is no plan at all — a user who has never planned a week
    does not have a post due, and inventing one would be a task the product
    made up.
    """
    if plan is None:
        return None
    for slot in plan.slots:
        if slot.slot_date != today:
            continue
        if slot.state in (SlotState.PUBLISHED, SlotState.SKIPPED):
            return None
        return str(slot.day)
    return None


def count_matches(
    rows: list[dict[str, Any]], *, last_seen: datetime | None
) -> tuple[int | None, int]:
    """(new since last seen, currently open).

    The first is `None` when `last_seen` is None. There is no point to measure
    "since" from, and both alternatives lie: everything-is-new invents a
    comparison, nothing-is-new hides a full feed.
    """
    active = [r for r in rows if str(r.get("status") or "") in DEFAULT_STATUSES]
    if last_seen is None:
        return None, len(active)

    fresh = 0
    for row in active:
        first_seen = _parse(row.get("first_seen"))
        if first_seen is not None and first_seen > last_seen:
            fresh += 1
    return fresh, len(active)


def load_signals(
    client: Any,
    user_id: str,
    profile: dict[str, Any],
    *,
    pending_fix: str | None = None,
    now: datetime | None = None,
) -> Signals:
    """Everything the ladder is allowed to see, read from real rows.

    A failure in any one source is logged and treated as "nothing to say about
    that rung" rather than propagated — Today going blank because the tracker
    query hiccuped would lose four working rungs to one broken one. The
    exception is that nothing here INVENTS a signal: a failed read produces
    absence, never a fabricated count.
    """
    moment = now or datetime.now(UTC)
    today = moment.date()

    tracker_rows: list[dict[str, Any]] = []
    try:
        res = client.table(TRACKER).select("*").eq("user_id", user_id).execute()
        tracker_rows = res.data if res and isinstance(res.data, list) else []
    except Exception:
        logger.exception("could not read the tracker for %s", user_id)

    job_rows: list[dict[str, Any]] = []
    try:
        res = client.table(JOBS).select("status,first_seen").eq("user_id", user_id).execute()
        job_rows = res.data if res and isinstance(res.data, list) else []
    except Exception:
        logger.exception("could not read the jobs feed for %s", user_id)

    plan = None
    try:
        plan = SupabasePlanStore(client).load(user_id, _monday_of(today))
    except Exception:
        logger.exception("could not read the week plan for %s", user_id)

    when, label = next_interview(tracker_rows, now=moment)
    fresh, open_count = count_matches(job_rows, last_seen=_parse(profile.get("lastSeenJobsAt")))

    return Signals(
        now=moment,
        next_interview_at=when,
        interview_label=label,
        post_due_today=post_due_today(plan, today=today),
        pending_fix=pending_fix,
        new_match_count=fresh,
        open_match_count=open_count,
    )


__all__ = [
    "JOBS",
    "OPEN_STAGES",
    "TRACKER",
    "count_matches",
    "load_signals",
    "next_interview",
    "post_due_today",
]
