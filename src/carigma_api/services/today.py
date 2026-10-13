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
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from carigma_api.services.applications import (
    OPEN_STAGES,
    next_interview,
    normalise_stage,
)
from carigma_api.services.applications import (
    TABLE as APPLICATIONS,
)
from carigma_api.services.jobs.feed import DEFAULT_STATUSES
from carigma_api.services.ladder import Signals
from carigma_api.services.posts import SlotState
from carigma_api.services.posts_store import SupabasePlanStore

logger = logging.getLogger(__name__)

JOBS = "jobs_feed"
SCORES = "score_history"

#: The seven cells of the week strip, Monday first.
WEEKDAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")

#: How far back the score delta looks. The spec's "+6 in 30 days".
DELTA_WINDOW = timedelta(days=30)


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


@dataclass(frozen=True)
class Sources:
    """The raw rows Today reads, with failure kept distinct from emptiness.

    **A read that failed is `None`; a read that found nothing is `[]`.** They
    were the same thing here — every exception was swallowed into an empty list
    — which is survivable for the ladder (a rung with nothing to say) and is
    NOT survivable for a signal row, where an unreadable table renders as a
    measured zero. "Cannot count" and "counted zero" are different facts.
    """

    now: datetime
    tracker: list[dict[str, Any]] | None
    jobs: list[dict[str, Any]] | None
    plan: Any | None
    #: `None` = the read failed. `[]` = this user has never been scored.
    scores: list[dict[str, Any]] | None
    #: Distinguishes "no plan for this week" from "could not read the plan",
    #: which a `plan` of `None` cannot do on its own.
    plan_read: bool


def read_sources(client: Any, user_id: str, *, now: datetime) -> Sources:
    """Every table Today touches, read once.

    Once, because `/today` renders the ladder AND the signal row from the same
    facts. Reading twice would let the primary card and the numbers beneath it
    disagree inside a single response.
    """
    today = now.date()

    tracker_rows: list[dict[str, Any]] | None
    try:
        res = (
            client.table(APPLICATIONS)
            .select("id,company,role,status,interview_on,closed_reason,updated_at")
            .eq("user_id", user_id)
            .execute()
        )
        tracker_rows = res.data if res and isinstance(res.data, list) else []
    except Exception:
        logger.exception("could not read the applications for %s", user_id)
        tracker_rows = None

    job_rows: list[dict[str, Any]] | None
    try:
        # Widened from `status,first_seen` so Today can show WHICH roles,
        # not only how many. A count is a number; a title and a company is
        # a reason to open the page.
        res = (
            client.table(JOBS)
            .select("id,title,company,match_score,status,first_seen")
            .eq("user_id", user_id)
            .execute()
        )
        job_rows = res.data if res and isinstance(res.data, list) else []
    except Exception:
        logger.exception("could not read the jobs feed for %s", user_id)
        job_rows = None

    plan = None
    plan_read = True
    try:
        plan = SupabasePlanStore(client).load(user_id, _monday_of(today))
    except Exception:
        logger.exception("could not read the week plan for %s", user_id)
        plan_read = False

    scores: list[dict[str, Any]] | None
    try:
        res = (
            client.table(SCORES)
            .select("score,created_at")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .limit(60)
            .execute()
        )
        scores = res.data if res and isinstance(res.data, list) else []
    except Exception:
        logger.exception("could not read the score history for %s", user_id)
        scores = None

    return Sources(
        now=now,
        tracker=tracker_rows,
        jobs=job_rows,
        plan=plan,
        scores=scores,
        plan_read=plan_read,
    )


def load_signals(
    client: Any,
    user_id: str,
    profile: dict[str, Any],
    *,
    pending_fix: str | None = None,
    now: datetime | None = None,
    sources: Sources | None = None,
) -> Signals:
    """Everything the ladder is allowed to see, read from real rows.

    A failure in any one source is logged and treated as "nothing to say about
    that rung" rather than propagated — Today going blank because the tracker
    query hiccuped would lose four working rungs to one broken one. The
    exception is that nothing here INVENTS a signal: a failed read produces
    absence, never a fabricated count.
    """
    moment = now or datetime.now(UTC)
    read = sources or read_sources(client, user_id, now=moment)

    when, label = next_interview(read.tracker or [], now=moment)
    fresh, open_count = count_matches(
        read.jobs or [], last_seen=_parse(profile.get("lastSeenJobsAt"))
    )

    return Signals(
        now=moment,
        next_interview_at=when,
        interview_label=label,
        post_due_today=post_due_today(read.plan, today=moment.date()),
        pending_fix=pending_fix,
        new_match_count=fresh,
        open_match_count=open_count,
    )


def score_signal(rows: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    """The current score and its 30-day movement, or nothing.

    Three different absences, and they are not interchangeable:

    - the read failed          -> `None` for the whole block
    - never scored             -> a value of `None`, which the surface says out loud
    - scored once, days ago    -> a value with `delta_30d: None`, because there is
      no earlier reading to subtract and `0` would claim "no movement"
    """
    if rows is None:
        return None
    readings = [
        (when, int(row["score"]))
        for row in rows
        if isinstance(row.get("score"), int | float)
        and (when := _parse(row.get("created_at"))) is not None
    ]
    if not readings:
        return {"value": None, "delta_30d": None, "measured_at": None}

    readings.sort(key=lambda pair: pair[0])
    when, latest = readings[-1]
    earlier = [value for at, value in readings[:-1] if at >= when - DELTA_WINDOW]
    return {
        "value": latest,
        "delta_30d": latest - earlier[0] if earlier else None,
        "measured_at": when.isoformat(),
    }


def pipeline_signal(rows: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    """How many applications are live, and how many are at interview.

    `saved` is excluded: a bookmark is not an application. So are `offer` and
    `closed`, which are both finished.
    """
    if rows is None:
        return None
    stages = [normalise_stage(r.get("status")) for r in rows]
    live = [stage for stage in stages if stage in OPEN_STAGES]
    return {
        "active": len(live),
        "interviewing": sum(1 for stage in live if stage == "interview"),
    }


def week_strip(plan: Any, *, today: date, plan_read: bool = True) -> list[dict[str, Any]] | None:
    """Seven cells, Monday first - the week at a glance.

    `None` when the plan could not be read. An empty week and an unreadable one
    both look like seven grey dots, so the difference has to survive here or it
    is gone.
    """
    if not plan_read:
        return None
    monday = _monday_of(today)
    by_day = {str(slot.day): slot for slot in (plan.slots if plan is not None else ())}
    cells: list[dict[str, Any]] = []
    for index, day in enumerate(WEEKDAYS):
        on = monday + timedelta(days=index)
        slot = by_day.get(day)
        cells.append(
            {
                "day": day,
                "date": on.isoformat(),
                # "none" is a day with no slot, which is most days for most
                # cadences. It is a real answer, not a missing value.
                "state": str(slot.state) if slot is not None else "none",
                "optional": bool(slot.optional) if slot is not None else False,
                "is_today": on == today,
            }
        )
    return cells


#: How many matches Today names. Two is a preview; five is the Jobs page.
PREVIEW = 2


def matches_preview(rows: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """The strongest couple of open matches, named.

    Sorted by match score, and a row without one sorts last rather than being
    treated as a zero — an unscored job is not a bad match.
    """
    if not rows:
        return []
    active = [r for r in rows if str(r.get("status") or "") in DEFAULT_STATUSES]
    active.sort(
        key=lambda r: r["match_score"] if isinstance(r.get("match_score"), int | float) else -1,
        reverse=True,
    )
    return [
        {
            "id": row.get("id"),
            "title": row.get("title"),
            "company": row.get("company"),
            "match_score": row.get("match_score"),
        }
        for row in active[:PREVIEW]
    ]


def signal_row(sources: Sources, *, new_matches: int | None, open_matches: int) -> dict[str, Any]:
    """Score, pipeline and matches - the three numbers Today may show.

    Spec 3.2. Each is the product's own record of something that happened, and
    each is `None` rather than zero when its source could not be read.
    """
    return {
        "score": score_signal(sources.scores),
        "pipeline": pipeline_signal(sources.tracker),
        "matches": None
        if sources.jobs is None
        else {
            "new": new_matches,
            "open": open_matches,
            "top": matches_preview(sources.jobs),
        },
    }


__all__ = [
    "JOBS",
    "OPEN_STAGES",
    "normalise_stage",
    "APPLICATIONS",
    "count_matches",
    "load_signals",
    "next_interview",
    "post_due_today",
    "Sources",
    "pipeline_signal",
    "read_sources",
    "score_signal",
    "matches_preview",
    "signal_row",
    "week_strip",
]
