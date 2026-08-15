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

from datetime import UTC, date, datetime
from typing import Any

#: Rows the user dismissed stay dismissed. "Not for me" is a decision, and
#: re-showing it would make the feedback pointless.
DEFAULT_STATUSES = ("active", "saved")


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


def _is_new(first_seen: Any, today: date) -> bool:
    if not isinstance(first_seen, str):
        return False
    try:
        return datetime.fromisoformat(first_seen.replace("Z", "+00:00")).date() == today
    except ValueError:
        return False


def present_job(row: dict[str, Any], *, today: date) -> dict[str, Any]:
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
        "first_seen": row.get("first_seen"),
        "is_new_today": _is_new(row.get("first_seen"), today),
        "status": row.get("status"),
        "has_apply_kit": bool(row.get("apply_kit")),
    }


def scan_summary(rows: list[dict[str, Any]], *, today: date) -> dict[str, Any]:
    """What the header line can truthfully say about the last scan.

    `last_scan_at` is `None` when no row carries a timestamp — which the
    surface must render as "never scanned", not as an old date.
    """
    seen: list[str] = [value for row in rows if isinstance(value := row.get("last_seen"), str)]
    new_today = sum(1 for row in rows if _is_new(row.get("first_seen"), today))
    sources: set[str] = set()
    for row in rows:
        detail = row.get("details")
        if isinstance(detail, dict) and detail.get("source"):
            sources.add(str(detail["source"]))
    return {
        "last_scan_at": max(seen) if seen else None,
        "new_count": new_today,
        "surfaced": len(rows),
        "sources": sorted(sources),
    }


def present_feed(rows: list[dict[str, Any]], *, now: datetime | None = None) -> dict[str, Any]:
    today = (now or datetime.now(UTC)).date()
    items = [present_job(row, today=today) for row in rows]
    # Best match first, and a row with no score sorts last rather than as zero.
    items.sort(key=lambda item: (item["match_score"] is None, -(item["match_score"] or 0)))
    return {"items": items, "scan": scan_summary(rows, today=today)}


__all__ = ["DEFAULT_STATUSES", "present_feed", "present_job", "scan_summary"]
