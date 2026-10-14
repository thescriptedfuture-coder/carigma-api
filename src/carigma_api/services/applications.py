"""The pipeline — V1's `job_applications`, read and written as V1 wrote it.

## Measured before mapping

    job_applications  13 rows, 1 user   company/role/status/interview_on/closed_reason
    application_events 11 rows, 1 user  application_id (UUID) -> job_applications.id
    interview_preps     2 rows, 1 user  application_id (UUID)
    tracker             0 rows          nothing has ever written to it

`tracker` is a V2-era table with plausible columns and no rows, and
`services/today.py` read it from the day the ladder was built. The read
succeeded and returned nothing, so the interview rung could not fire and the
pipeline stat reported `0 active` to the one user with thirteen applications.
Counting the rows was the whole diagnosis.

## One vocabulary, V1's

`status` not `stage`; `role` not `title`; `interview_on` not `interview_at`;
and `interview_on` is a **date**. A legacy `rejected` still exists in the live
data and normalises to `closed`, so `normalise_stage` is load-bearing.

Event names are V1's too — `stage_{new}`, `note`, `prep_built` — because the
eleven existing rows use them and a second convention would make the timeline
disagree with itself halfway down.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from typing import Any

logger = logging.getLogger(__name__)

TABLE = "job_applications"
EVENTS = "application_events"

#: The five stages, in board order. `resumeiq/db.py:JOB_STAGES`.
JOB_STAGES: tuple[str, ...] = ("saved", "applied", "interview", "offer", "closed")

#: Stages where an application is still live. `saved` is a bookmark, not an
#: application; `offer` and `closed` are both finished, in opposite directions.
OPEN_STAGES: tuple[str, ...] = ("applied", "interview")

#: What V1 offers when a row is closed, plus the one it never had.
#:
#: V1's four are `Rejected, Withdrew, Ghosted, Accepted elsewhere` — every one
#: of them a way for the search to end badly or elsewhere. **There was no way
#: to say you took THIS one**, which is the best outcome the product has and
#: the only one it could not record. "Accepted elsewhere" sitting alone implies
#: the offer in front of you is never the one you take.
#:
#: Named "Accepted this offer" rather than "Accepted" so it cannot be misread
#: next to its neighbour at a glance.
CLOSED_REASONS: tuple[str, ...] = (
    "Accepted this offer",
    "Rejected",
    "Withdrew",
    "Ghosted",
    "Accepted elsewhere",
)

#: The reason that means the search ENDED WELL. Kept as a name rather than a
#: string literal at the call sites, because three surfaces ask the question.
ACCEPTED = "Accepted this offer"

#: How long a closed application stays in the column before it folds away.
#:
#: FOLDS, not deletes. Removing someone's record of their own job search to
#: keep a column short trades a permanent loss against a presentation problem —
#: and "did I apply to this company before?" is a question the row is the only
#: answer to. Nothing is scheduled and nothing is removed; the view stops
#: listing them and says how many it is holding.
FOLD_CLOSED_AFTER_DAYS = 30

#: V1 nudges a follow-up after ten days in `applied`. Ported rather than picked.
FOLLOW_UP_DAYS = 10

STAGE_LABELS: dict[str, str] = {
    "saved": "Saved",
    "applied": "Applied",
    "interview": "Interview",
    "offer": "Offer",
    "closed": "Closed",
}


def normalise_stage(status: Any) -> str:
    """Any stored status onto the five-stage model.

    `rejected` predates the `closed` + reason split and one live row still
    carries it, so this is load-bearing rather than defensive. Ported from
    `resumeiq/db.py:normalize_stage` so both products read the column the same
    way instead of twice.
    """
    value = str(status or "").lower()
    if value == "rejected":
        return "closed"
    return value if value in JOB_STAGES else "saved"


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        logger.error("unparseable timestamp %r on an application", value)
        return None
    # `interview_on` is a DATE. Naive, it compares to an aware `now` with
    # TypeError — which would take a whole screen down rather than one row.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def days_in_stage(row: dict[str, Any], *, now: datetime) -> int | None:
    """How long since this row last moved, from `updated_at`.

    `None` when neither timestamp parses. Not `0`: "we cannot tell" and "it
    moved today" are different, and only one of them should suppress a
    follow-up nudge.
    """
    when = _parse(row.get("updated_at")) or _parse(row.get("created_at"))
    if when is None:
        return None
    return max(0, (now - when).days)


def needs_follow_up(row: dict[str, Any], *, now: datetime) -> bool:
    """Ten days in `applied` with no movement. V1's rule, V1's threshold."""
    if normalise_stage(row.get("status")) != "applied":
        return False
    days = days_in_stage(row, now=now)
    return days is not None and days >= FOLLOW_UP_DAYS


def present(row: dict[str, Any], *, now: datetime) -> dict[str, Any]:
    """One application as the client reads it. Nothing here invents a value."""
    stage = normalise_stage(row.get("status"))
    return {
        "id": row.get("id"),
        "company": row.get("company"),
        "role": row.get("role"),
        "stage": stage,
        "stage_label": STAGE_LABELS[stage],
        # Free text as V1 stored it, or nothing. Never "Competitive".
        "salary": row.get("salary") or None,
        "link": row.get("link") or None,
        "notes": row.get("notes") or None,
        "interview_on": row.get("interview_on") or None,
        # Only meaningful on a closed row, and `None` there means the row was
        # closed before we asked why — not that no reason exists.
        "closed_reason": row.get("closed_reason") if stage == "closed" else None,
        "days_in_stage": days_in_stage(row, now=now),
        "needs_follow_up": needs_follow_up(row, now=now),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def board(rows: list[dict[str, Any]], *, now: datetime) -> list[dict[str, Any]]:
    """The five columns, in order, each with its rows and its count.

    Every stage appears even when empty: a board that hides its empty columns
    teaches a different shape of the pipeline every time it is opened.

    **Closed folds, and only closed.** A search that runs for a year closes
    dozens of applications and the column grows without bound, so rows closed
    more than `FOLD_CLOSED_AFTER_DAYS` ago move to `folded` instead of `items`.
    The count still counts them — the column says how many it is holding, and
    the client can show them. Nothing is deleted and nothing is scheduled.
    """
    presented = [present(row, now=now) for row in rows]
    columns: list[dict[str, Any]] = []
    for stage in JOB_STAGES:
        here = [item for item in presented if item["stage"] == stage]
        folded: list[dict[str, Any]] = []
        if stage == "closed":
            fresh: list[dict[str, Any]] = []
            for item in here:
                days = item["days_in_stage"]
                # `None` days stays visible. We do not know when it closed, and
                # hiding a row on the strength of a timestamp we could not read
                # is the falsy-zero mistake wearing a different hat.
                (folded if days is not None and days > FOLD_CLOSED_AFTER_DAYS else fresh).append(
                    item
                )
            here = fresh
        columns.append(
            {
                "stage": stage,
                "label": STAGE_LABELS[stage],
                "count": len(here) + len(folded),
                "items": here,
                "folded": folded,
            }
        )
    return columns


def summary(rows: list[dict[str, Any]], *, now: datetime) -> dict[str, Any]:
    """What the board says about itself, for Today and for the header."""
    stages = [normalise_stage(row.get("status")) for row in rows]
    return {
        "total": len(rows),
        "active": sum(1 for stage in stages if stage in OPEN_STAGES),
        "interviewing": sum(1 for stage in stages if stage == "interview"),
        "needs_follow_up": sum(1 for row in rows if needs_follow_up(row, now=now)),
    }


def event_for(stage: str, *, reason: str | None = None) -> dict[str, str]:
    """The row `application_events` gets when a stage changes.

    V1's names, because eleven rows already use them: `stage_{new}`, with the
    closed reason carried in the note exactly as V1 wrote it. A second naming
    convention would make one timeline disagree with itself halfway down.
    """
    if stage == "closed" and reason:
        return {"event_type": "stage_closed", "note": f"Closed — {reason}"}
    return {"event_type": f"stage_{stage}", "note": STAGE_LABELS[stage]}


def next_interview(
    rows: list[dict[str, Any]], *, now: datetime
) -> tuple[datetime | None, str, str | None]:
    """The soonest interview still ahead, and what to call it.

    Finished applications are excluded even when they carry a date — one live
    row is `closed` with an `interview_on` in July. An interview you already
    had, or for a role that is gone, is not something to prepare for.
    """
    upcoming: list[tuple[datetime, str, str | None]] = []
    for row in rows:
        if normalise_stage(row.get("status")) not in OPEN_STAGES:
            continue
        when = _parse(row.get("interview_on"))
        if when is None or when < now:
            continue
        label = ", ".join(p for p in (row.get("company"), row.get("role")) if p)
        app_id = row.get("id")
        upcoming.append((when, label, str(app_id) if app_id else None))

    if not upcoming:
        return None, "", None
    return min(upcoming, key=lambda found: found[0])


def valid_interview_date(value: Any) -> date | None:
    """A date, or nothing. Never a datetime, because the column is a date."""
    parsed = _parse(value)
    return parsed.date() if parsed else None


__all__ = [
    "ACCEPTED",
    "CLOSED_REASONS",
    "FOLD_CLOSED_AFTER_DAYS",
    "EVENTS",
    "FOLLOW_UP_DAYS",
    "JOB_STAGES",
    "OPEN_STAGES",
    "STAGE_LABELS",
    "TABLE",
    "board",
    "days_in_stage",
    "event_for",
    "needs_follow_up",
    "next_interview",
    "normalise_stage",
    "present",
    "summary",
    "valid_interview_date",
]
