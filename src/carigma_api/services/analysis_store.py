"""The last Profile Analyst reading, kept whole — V1's `results.profile_data`.

## Why Signal could show nothing

V2 stored only the number (`score_history.score`). The breakdown, the summary,
the rewrites and the quick wins lived in the response to `POST /score/compute`
and nowhere else, so Signal opened on "The Profile Analyst hasn't read your
profile in this session. We won't show a score we haven't just measured" —
for a user with twenty-two readings. V1 had kept the whole analysis all along,
in `results.profile_data`, and renders it beside the date it was measured.

A stored reading WITH ITS DATE is a record, not a fabrication. The honesty
moves from refusing to render into the label: "Measured 11 Aug · 69 days ago".

## One row, two writers

`results` is V1's table (one row per user, `user_id` unique). V1 writes the
WHOLE row from session state; this writes two columns and leaves `posts` and
`jobs` alone. Whichever version ran the analyst last is what both show — the
same last-writer-wins V1 already has with itself across two tabs.

- `profile_data` gains `measuredAt` (ISO, UTC). V1 reads the keys it knows
  and ignores the rest.
- `last_run` is TEXT holding JSON (`{"profile": "11 Aug 2026, 09:42 AM",
  "jobs": ...}`) — read, the `profile` key replaced, written back whole, so
  V1's other agents keep their stamps. Replacing it outright would erase them:
  a jsonb-shaped update replaces the whole value.

## When a reading was taken

`measuredAt` when V2 wrote it — exact. Otherwise V1's `last_run.profile`
stamp, which V1 wrote with the server's naive `datetime.now()`: the DAY is
trustworthy, the hour's timezone is not, so only the day is reported.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from carigma_api.services.clock import local

logger = logging.getLogger(__name__)

TABLE = "results"

#: V1's stamp format (app.py, every agent run).
V1_STAMP = "%d %b %Y, %I:%M %p"


@dataclass(frozen=True)
class StoredAnalysis:
    analysis: dict[str, Any]
    #: Exact, when V2 took the reading.
    measured_at: datetime | None
    #: The day, whenever it is known at all.
    measured_on: date | None


def _measured(profile_data: dict[str, Any], last_run: Any) -> tuple[datetime | None, date | None]:
    raw = profile_data.get("measuredAt")
    if isinstance(raw, str):
        try:
            at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            logger.error("unreadable measuredAt %r on a %s row", raw, TABLE)
        else:
            at = at if at.tzinfo else at.replace(tzinfo=UTC)
            return at, local(at).date()
    stamps = _stamps(last_run)
    stamp = stamps.get("profile")
    if isinstance(stamp, str):
        try:
            return None, datetime.strptime(stamp, V1_STAMP).date()
        except ValueError:
            logger.error("unreadable V1 profile stamp %r on a %s row", stamp, TABLE)
    return None, None


def _stamps(last_run: Any) -> dict[str, Any]:
    """V1's `last_run`: JSON in a TEXT column, or — on very old rows — a bare
    timestamp string, which `db_to_results` keeps as a string."""
    if isinstance(last_run, dict):
        return dict(last_run)
    if isinstance(last_run, str) and last_run.strip().startswith("{"):
        try:
            parsed = json.loads(last_run)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


class AnalysisStore:
    """Reads and writes through the CALLER's client, so RLS applies."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def _row(self, user_id: str) -> dict[str, Any] | None:
        res = (
            self._client.table(TABLE)
            .select("profile_data,last_run")
            .eq("user_id", user_id)
            .execute()
        )
        rows = res.data if res and isinstance(res.data, list) else []
        return rows[0] if rows and isinstance(rows[0], dict) else None

    def latest(self, user_id: str) -> StoredAnalysis | None:
        """The last reading, or None if there has never been one. Raises if
        the row cannot be read — "could not look" is not "never scored"."""
        row = self._row(user_id)
        if row is None:
            return None
        profile_data = row.get("profile_data")
        # V1's column default is `{}`: a row exists for anyone who ran ANY
        # agent, and only a reading with a number in it is a reading.
        if not isinstance(profile_data, dict) or not isinstance(
            profile_data.get("profileScore"), int | float
        ):
            return None
        at, on = _measured(profile_data, row.get("last_run"))
        return StoredAnalysis(analysis=profile_data, measured_at=at, measured_on=on)

    def save(self, user_id: str, analysis: dict[str, Any], *, at: datetime) -> None:
        """Keep a reading whole, stamped. Only `profile_data` and `last_run`."""
        row = self._row(user_id)
        stamps = _stamps(row.get("last_run") if row else None)
        stamps["profile"] = local(at).strftime(V1_STAMP)
        self._client.table(TABLE).upsert(
            {
                "user_id": user_id,
                "profile_data": {**analysis, "measuredAt": at.isoformat()},
                "last_run": json.dumps(stamps),
                "updated_at": at.isoformat(),
            },
            on_conflict="user_id",
        ).execute()


__all__ = ["TABLE", "V1_STAMP", "AnalysisStore", "StoredAnalysis"]
