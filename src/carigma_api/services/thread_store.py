"""Thread decisions: what the user has dismissed or snoozed.

## Derive the candidates, persist the DECISIONS

Thread items are computed fresh on every load from the same signals the ladder
reads — a post due, a fix waiting, matches surfaced. Storing the items
themselves would mean a second copy of state that drifts from the first.

What cannot be derived is what the user did about them. A dismissal and a
snooze are facts about a person's intent, and nothing else in the system knows
them.

## Which makes the round trip the whole point

If a snooze does not survive the next request, the item reappears — and an
item that comes back after being dismissed is worse than one that was never
dismissible. The user told us something and we lost it, visibly, on the
surface where they told us.

So the tests here write and then read back through a SEPARATE call. That is
also what `/thread` returning a well-formed payload for a phase, while storing
nothing, looked like from the outside.

## `snooze_until` is a time, not a flag

"Snoozed" with no date is a state nothing can leave. A row that says WHEN it
stops applying expires by arithmetic rather than by someone remembering to
clear it.
"""

from __future__ import annotations

import logging
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

logger = logging.getLogger(__name__)

TABLE = "thread_decisions"


class Decision(StrEnum):
    #: Gone for good. The user said this is not for them.
    DISMISSED = "dismissed"
    #: Gone until `snooze_until`. Not the same as dismissed, and a surface that
    #: treated them alike would either nag someone who said no or silently drop
    #: something they asked to see later.
    SNOOZED = "snoozed"


class ThreadStore(Protocol):
    def decisions(self, user_id: str) -> dict[str, dict[str, Any]]: ...
    def record(
        self,
        user_id: str,
        item_key: str,
        decision: Decision,
        *,
        snooze_until: datetime | None = None,
    ) -> None: ...


def is_suppressed(row: dict[str, Any], *, now: datetime) -> bool:
    """Whether this decision still hides its item.

    A dismissal is permanent. A snooze is not, and one whose time has passed
    must stop applying on its own — otherwise "snooze" is a slower dismiss.
    """
    decision = str(row.get("decision") or "")
    if decision == Decision.DISMISSED:
        return True
    if decision != Decision.SNOOZED:
        return False

    until = row.get("snooze_until")
    if not isinstance(until, str) or not until:
        # A snooze with no end is a state nothing can leave. Treating it as
        # expired errs toward showing the user their own item back, which is
        # recoverable; the other way silently swallows it forever.
        logger.warning("snoozed row for %s has no snooze_until", row.get("user_id"))
        return False
    try:
        return datetime.fromisoformat(until.replace("Z", "+00:00")) > now
    except ValueError:
        logger.error("unparseable snooze_until %r", until)
        return False


class SupabaseThreadStore:
    """Reads and writes through the CALLER's client, so RLS applies."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def decisions(self, user_id: str) -> dict[str, dict[str, Any]]:
        """item_key -> the row. One decision per item, by construction."""
        res = self._client.table(TABLE).select("*").eq("user_id", user_id).execute()
        rows = res.data if res and isinstance(res.data, list) else []
        return {
            str(r["item_key"]): dict(r) for r in rows if isinstance(r, dict) and r.get("item_key")
        }

    def record(
        self,
        user_id: str,
        item_key: str,
        decision: Decision,
        *,
        snooze_until: datetime | None = None,
    ) -> None:
        # Upsert on `(user_id, item_key)`: deciding twice about one item
        # replaces the decision rather than stacking two, so a snooze followed
        # by a dismiss ends as a dismiss.
        self._client.table(TABLE).upsert(
            {
                "user_id": user_id,
                "item_key": item_key,
                "decision": str(decision),
                "snooze_until": snooze_until.isoformat() if snooze_until else None,
            },
            on_conflict="user_id,item_key",
        ).execute()


def surviving(
    items: list[dict[str, Any]], decisions: dict[str, dict[str, Any]], *, now: datetime
) -> list[dict[str, Any]]:
    """The items still worth showing, given what the user has already said."""
    return [
        item
        for item in items
        if not is_suppressed(decisions.get(str(item.get("key") or ""), {}), now=now)
    ]


__all__ = ["TABLE", "Decision", "SupabaseThreadStore", "ThreadStore", "is_suppressed", "surviving"]
