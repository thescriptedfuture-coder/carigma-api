"""Reading the stored jobs feed — the rows V1 has been writing for months.

## What is actually in there

Measured, not assumed: 93 rows, 6 users, and **three generations of shape**.

    all 93   applyLinks, keyRequirements, cvTweaks, greenFlags,
             outreachStrategy, deadline, urgency        (V1's original)
    83       details.apply_url, details.source
    66       apply_options, matched_skills, missing_skills,
             posted_at, description, salary_predicted   (the P1 normaliser)

So ten rows have no apply URL at all, and twenty-seven were never analysed for
skill overlap. Any mapping that defaults those to `""` or `[]` states
something false about a real user's real job.

## The rule this file exists to keep

**A field the row never had is `None`, never a default.**

`matched_skills: []` says "we compared your skills and found no overlap".
`matched_skills: None` says "this row predates that analysis". They render
differently and they mean different things, and the older rows are entitled to
the second one. Same lesson as `gist_for_email` returning `None` rather than
`[]`, one layer down.

## `applyLinks` is not an apply link

V1's `applyLinks` is a dict keyed by job board — `{shine: ..., naukri: ...,
linkedin: ...}` — for a single job. Six boards do not host six postings of one
role; those are **searches**, which is V1's bug 1.2 exactly: a keyword search
dressed up as "Apply", wasting the user's time and eroding trust.

They are surfaced as `search_links`, never as `apply_url`, and the model layer
already legislates this: "Search links live in `search_links` and must be
labelled as searches." A row with no real posting gets `apply_url: None` and
says so.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

#: Rows the user dismissed stay dismissed. "Not for me" is a decision, and
#: re-showing it would make the feedback pointless.
DEFAULT_STATUSES = ("active", "saved")

#: Spec 3.4 and V1 (`jobs_feed.is_new_today`): "first seen within 24h". The
#: first V2 version compared UTC calendar dates, which made a 05:00 IST scan
#: "yesterday" and could not agree with V1 about the same row.
NEW_FOR = timedelta(hours=24)

#: How long a posting stays in the main list after the last sign it was open.
#:
#: ## Why a window, and why this one
#:
#: Measured 2026-10-17: Ravi's feed held 58 active postings, 45 of them first
#: seen in July and August and never seen again. The list was mostly
#: history, and the thirteen live ones were scattered through it by score.
#:
#: The source's own closing date is used when it gives one. Most do not, so
#: the fallback is the newest evidence the posting was alive — the source's
#: posting date, or the last scan that returned it. Three weeks without either
#: is past most boards' active life, and well past the point an application
#: tends to be read.
#:
#: "Older" is a read-time fold, not a deletion and not a cron. The row stays,
#: so a posting that shows up again is recognised rather than announced as
#: new, and a dismissal stays a dismissal.
STALE_AFTER = timedelta(days=21)


def _list_or_none(value: Any) -> list[str] | None:
    """`None` when the field is absent, a list when it is present.

    The distinction the older rows depend on. `value or None` would collapse a
    genuine empty list into "never analysed", which is the same conflation in
    the other direction.
    """
    if value is None:
        return None
    if isinstance(value, list):
        return [str(item) for item in value]
    return None


def _search_links(details: dict[str, Any]) -> list[dict[str, str]]:
    """V1's `applyLinks`, presented as what they are."""
    raw = details.get("applyLinks")
    if not isinstance(raw, dict):
        return []
    return [
        {"publisher": str(pub), "url": str(url)}
        for pub, url in raw.items()
        if isinstance(url, str) and url.startswith("http")
    ]


def _apply_options(details: dict[str, Any]) -> list[dict[str, Any]]:
    raw = details.get("apply_options")
    if not isinstance(raw, list):
        return []
    out = []
    for option in raw:
        if isinstance(option, dict) and isinstance(option.get("url"), str):
            out.append(
                {
                    "publisher": str(option.get("publisher") or ""),
                    "url": option["url"],
                    "is_direct": bool(option.get("is_direct")),
                }
            )
    return out


def _when(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _is_new(first_seen: Any, now: datetime) -> bool:
    seen = _when(first_seen)
    return seen is not None and timedelta(0) <= now - seen <= NEW_FOR


def _detail(row: dict[str, Any], key: str) -> Any:
    """A `details` key, or the same key at the top level.

    Today reads `posted_at:details->>posted_at` rather than the whole jsonb,
    which can carry a full job description per row; the Jobs page reads
    `details`. Both shapes mean the same posting.
    """
    if key in row:
        return row[key]
    raw = row.get("details")
    return raw.get(key) if isinstance(raw, dict) else None


def is_current(row: dict[str, Any], *, now: datetime) -> bool:
    """Whether a posting belongs in the main list, or in "older".

    In order, and the first rule that applies decides:

    1. **Saved** stays. The user said so.
    2. **Has an apply kit** stays. It was paid for, and its owner may be
       mid-application.
    3. **The source's closing date**, when it published one, decides both ways
       — a posting open until December stays even if no scan has seen it.
    4. Otherwise the **newest evidence it was open** — posted, or returned by a
       scan — within `STALE_AFTER`.
    5. **No evidence at all** stays. Hiding needs a reason; "we cannot tell"
       is not one.
    """
    if str(row.get("status") or "") == "saved":
        return True
    if row.get("apply_kit") or row.get("kit_created_at"):
        return True

    closes = _when(_detail(row, "expires_at"))
    if closes is not None:
        return closes > now

    evidence = [
        when
        for when in (
            _when(row.get("last_seen")),
            _when(_detail(row, "posted_at")),
            _when(row.get("first_seen")),
        )
        if when is not None
    ]
    if not evidence:
        return True
    return now - max(evidence) <= STALE_AFTER


def present_job(row: dict[str, Any], *, now: datetime) -> dict[str, Any]:
    """One stored row, as the client reads it.

    Every optional field is passed through as `None` when the row does not
    carry it. Nothing here invents a value.
    """
    raw = row.get("details")
    details: dict[str, Any] = raw if isinstance(raw, dict) else {}
    apply_url = details.get("apply_url")

    return {
        "id": row.get("id"),
        "title": row.get("title"),
        "company": row.get("company"),
        "location": row.get("location"),
        # V1 leaves this null on plenty of rows and the chip is omitted rather
        # than "Full-time" invented.
        "job_type": row.get("job_type"),
        # Free text as the source published it, or nothing. Never "Competitive".
        "salary": row.get("salary"),
        # A predicted salary is not a published one. V1 stored the flag; rows
        # without it are not therefore "published", so this stays None.
        "salary_is_estimated": details.get("salary_predicted"),
        "match_score": row.get("match_score"),
        "why_match": row.get("why_match"),
        "matched_skills": _list_or_none(details.get("matched_skills")),
        "missing_skills": _list_or_none(details.get("missing_skills")),
        "key_requirements": _list_or_none(details.get("keyRequirements")),
        # None, not "", when no real posting is known for this row.
        "apply_url": apply_url if isinstance(apply_url, str) and apply_url else None,
        "apply_options": _apply_options(details),
        # Separate field, separate word. These are searches.
        "search_links": _search_links(details),
        "publisher": details.get("publisher"),
        "source": details.get("source"),
        "posted_at": details.get("posted_at"),
        # The source's closing date. Usually None — most boards publish none.
        "expires_at": details.get("expires_at"),
        "first_seen": row.get("first_seen"),
        # The last scan that returned it — what "older" is measured from.
        "last_seen": row.get("last_seen"),
        "is_new_today": _is_new(row.get("first_seen"), now),
        "status": row.get("status"),
        "has_apply_kit": bool(row.get("apply_kit")),
    }


def scan_summary(
    rows: list[dict[str, Any]], *, now: datetime, current: int | None = None
) -> dict[str, Any]:
    """What the header line can truthfully say about the last scan.

    `last_scan_at` is `None` when no row carries a timestamp — which the
    surface must render as "never scanned", not as an old date. `surfaced`
    counts the main list, so it agrees with what is on screen.
    """
    seen: list[str] = [value for row in rows if isinstance(value := row.get("last_seen"), str)]
    new_today = sum(1 for row in rows if _is_new(row.get("first_seen"), now))
    sources: set[str] = set()
    for row in rows:
        detail = row.get("details")
        if isinstance(detail, dict) and detail.get("source"):
            sources.add(str(detail["source"]))
    return {
        "last_scan_at": max(seen) if seen else None,
        "new_count": new_today,
        "surfaced": len(rows) if current is None else current,
        "sources": sorted(sources),
    }


def _by_score(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Best match first, and a row with no score sorts last rather than as zero.
    return sorted(
        items, key=lambda item: (item["match_score"] is None, -(item["match_score"] or 0))
    )  # falsy-ok: None already sorts last via the first tuple element


def present_feed(rows: list[dict[str, Any]], *, now: datetime | None = None) -> dict[str, Any]:
    """The main list, and the older postings folded beneath it.

    `older` is sent, not dropped: the surface shows how many and lets the user
    open them. A list that silently loses forty rows is the same failure as one
    that silently keeps them.
    """
    moment = now or datetime.now(UTC)
    current: list[dict[str, Any]] = []
    older: list[dict[str, Any]] = []
    for row in rows:
        (current if is_current(row, now=moment) else older).append(present_job(row, now=moment))
    return {
        "items": _by_score(current),
        "older": _by_score(older),
        # The rule's own number, so the surface's sentence cannot drift from it.
        "older_after_days": STALE_AFTER.days,
        "scan": scan_summary(rows, now=moment, current=len(current)),
    }


__all__ = [
    "DEFAULT_STATUSES",
    "NEW_FOR",
    "STALE_AFTER",
    "is_current",
    "present_feed",
    "present_job",
    "scan_summary",
]
