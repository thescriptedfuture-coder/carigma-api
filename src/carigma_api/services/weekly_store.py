"""Weekly contracts, in the database instead of a module dict.

`routes/weekly.py` held every contract in `_CONTRACTS: dict[str, dict[str,
WeeklyContract]]` with a comment promising persistence would land later. It
did not. The surface is marked Done, returns well-formed payloads, and stores
nothing — one restart loses every approved contract, and a second instance
gives two users different answers to the same question.

## Why this is required for the sweep, not just tidiness

The Sunday sweep asks "which contracts were never answered?" — a question
about all users, from a process that serves none of them. A module dict is
per-process and per-request-path, so a cron sweeping it would find an empty
one and truthfully report that nothing needed adopting. The sweep is only
meaningful over shared storage.

## Shape

One row per user per week (`unique (user_id, week_start)`), so a second
proposal for the same week updates rather than duplicating. `items` and
`accepted_kinds` are JSON because nothing filters inside them in SQL.

`decided_at` is what separates "took nothing" from "never decided" — an empty
`accepted_kinds` means both, and only the timestamp tells them apart.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Protocol

from carigma_api.services.weekly import (
    Cadence,
    ContractItem,
    ContractState,
    WeeklyContract,
)

logger = logging.getLogger(__name__)

TABLE = "weekly_review"


class ContractStore(Protocol):
    def history(self, user_id: str) -> list[WeeklyContract]: ...
    def load(self, user_id: str, week_start: date) -> WeeklyContract | None: ...
    def save(self, user_id: str, contract: WeeklyContract) -> None: ...
    def load_review(self, user_id: str, week_start: date) -> dict[str, Any] | None: ...
    def save_review(self, user_id: str, week_start: date, earned: dict[str, Any]) -> None: ...


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        # A timestamp we cannot parse is not "never decided". Returning None
        # here would turn a decided contract back into an open proposal and
        # make the sweep adopt a week the user already answered.
        logger.error("unparseable timestamp %r on a %s row", value, TABLE)
        raise


def to_row(user_id: str, contract: WeeklyContract) -> dict[str, Any]:
    return {
        "user_id": user_id,
        "week_start": contract.week_start.isoformat(),
        "state": str(contract.state),
        # Written from the DATACLASS, not from memory of it. The first draft
        # invented a `label` field; `ContractItem` has `day`, `summary` and
        # `cost_credits`. mypy caught it here, which is the boundary this
        # project keeps getting wrong when the two sides are only prose apart.
        "items": [
            {
                "kind": i.kind,
                "day": i.day,
                "summary": i.summary,
                "minutes": i.minutes,
                "cost_credits": i.cost_credits,
            }
            for i in contract.items
        ],
        "accepted_kinds": list(contract.accepted_kinds),
        "cadence": str(contract.cadence),
        "proposed_at": _iso(contract.proposed_at),
        "decided_at": _iso(contract.decided_at),
    }


def from_row(row: dict[str, Any]) -> WeeklyContract:
    raw_items = row.get("items")
    items = tuple(
        ContractItem(
            kind=str(item.get("kind") or ""),
            day=str(item.get("day") or ""),
            summary=str(item.get("summary") or ""),
            minutes=int(
                item.get("minutes") or 0
            ),  # falsy-ok: an item with no minutes costs zero minutes
            cost_credits=int(
                item.get("cost_credits") or 0
            ),  # falsy-ok: an item with no cost IS free
        )
        for item in (raw_items if isinstance(raw_items, list) else [])
        if isinstance(item, dict)
    )
    accepted = row.get("accepted_kinds")
    return WeeklyContract(
        week_start=date.fromisoformat(str(row["week_start"])),
        # Both columns are NOT NULL with a default and a check constraint, so
        # `or "proposed"` was unreachable — and it stated a policy nobody would
        # choose: that a state we cannot read is an OPEN PROPOSAL. A declined
        # week coming back as proposed is a week the sweep then auto-adopts,
        # putting a plan in force that the user explicitly refused. Reading it
        # strictly means a corrupt row fails loudly instead of quietly
        # reversing a decision.
        state=ContractState(str(row["state"])),
        items=items,
        proposed_at=_dt(row.get("proposed_at")),
        decided_at=_dt(row.get("decided_at")),
        cadence=Cadence(str(row["cadence"])),
        # NULL and `[]` both arrive as "no kinds", and that is correct: three
        # states have empty `accepted_kinds` — proposed, declined and
        # auto_adopted. What separates them is `state` and `decided_at`, never
        # the emptiness of this list, so nothing downstream may infer from it.
        accepted_kinds=tuple(str(k) for k in accepted) if isinstance(accepted, list) else (),
    )


class SupabaseContractStore:
    """Reads and writes through the CALLER's client, so RLS applies."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def history(self, user_id: str) -> list[WeeklyContract]:
        res = (
            self._client.table(TABLE)
            .select("*")
            .eq("user_id", user_id)
            .order("week_start", desc=False)
            .execute()
        )
        rows = res.data if res and isinstance(res.data, list) else []
        return [from_row(row) for row in rows if isinstance(row, dict)]

    def load(self, user_id: str, week_start: date) -> WeeklyContract | None:
        res = (
            self._client.table(TABLE)
            .select("*")
            .eq("user_id", user_id)
            .eq("week_start", week_start.isoformat())
            .execute()
        )
        rows = res.data if res and isinstance(res.data, list) else []
        return from_row(rows[0]) if rows and isinstance(rows[0], dict) else None

    def save(self, user_id: str, contract: WeeklyContract) -> None:
        # Upsert on the unique key. A re-proposal for the same week must not
        # create a second row — two contracts for one week would make
        # `consecutive_lapses` see a history that never happened.
        self._client.table(TABLE).upsert(
            to_row(user_id, contract), on_conflict="user_id,week_start"
        ).execute()

    def load_review(self, user_id: str, week_start: date) -> dict[str, Any] | None:
        """What the review EARNED, or None if no review was ever built.

        `facts` is nullable on purpose, and the two empty-looking values are
        different weeks:

        - `NULL` — no review was built. Nothing ran, or the week has not
          closed. The surface shows the day-one promise.
        - `{"facts": []}` — a review WAS built and the week produced nothing
          worth reporting. That is a real answer, and it must not be redrawn
          as "your first review builds Sunday".

        Same trap as the Posts port, where a paused week read as never-planned.
        A falsy check on the column collapses them.
        """
        res = (
            self._client.table(TABLE)
            .select("facts")
            .eq("user_id", user_id)
            .eq("week_start", week_start.isoformat())
            .execute()
        )
        rows = res.data if res and isinstance(res.data, list) else []
        if not rows or not isinstance(rows[0], dict):
            return None
        earned = rows[0].get("facts")
        if earned is None:
            return None
        if not isinstance(earned, dict):
            # A `facts` that is not the shape we write is corrupt, not empty.
            logger.error("unreadable review payload on %s for %s", TABLE, user_id)
            return None
        return earned

    def save_review(self, user_id: str, week_start: date, earned: dict[str, Any]) -> None:
        """Store what the week EARNED, never the rendered payload.

        Only the facts, the streak and any milestone go in. The week label and
        the reading estimate are derived at read time, because they are copy —
        freezing them in the database means a wording change needs a data
        migration, and rows written before the change keep saying the old thing
        forever.
        """
        self._client.table(TABLE).upsert(
            {
                "user_id": user_id,
                "week_start": week_start.isoformat(),
                "facts": earned,
            },
            on_conflict="user_id,week_start",
        ).execute()


__all__ = ["TABLE", "ContractStore", "SupabaseContractStore", "from_row", "to_row"]
