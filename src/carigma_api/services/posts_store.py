"""The content loop, in the database instead of a module dict.

`routes/posts.py` held every week plan in `_PLANS: dict[str, dict[str,
WeekPlan]]`, with a comment promising persistence "lands with P4-2". It did
not. The surface is marked Done, returns well-formed payloads, and stores
nothing — one restart loses every draft, publish mark and skip reason.

## One row per SLOT, not one blob per week

This is the whole reason the schema looks the way it does, and it is worth
being explicit about.

A week stored as one JSON document makes "mark Thursday published" a
read-modify-write of the entire week. Two tabs open — or a phone and a laptop —
and the second write silently discards the first: the user marks Monday
published on their phone, skips Thursday on their laptop, and whichever
request lands second erases the other. Nothing errors. The lost edit simply is
not there.

Row-per-slot makes those two writes touch different rows, so both survive.
`unique (user_id, week_start, day)` is the upsert target, so a double-submit
updates rather than creating a second Thursday.

The week's own facts — cadence, streak, pause — live in `content_weeks`,
because they belong to the week rather than to any day, and putting them on
every slot row would mean seven places to disagree about the streak.

## The port changed no behaviour

Asked the two questions that found three unreachable states and a caller-less
function in the weekly contract: **no SlotState value is unwritable, and no
public function here is uncalled.** Posts survives contact with real storage
as written, so this is a translation rather than a redesign.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Protocol

from carigma_api.services.posts import (
    Draft,
    SkipReason,
    Slot,
    SlotState,
    WeekPlan,
    default_week,
)

logger = logging.getLogger(__name__)

SLOTS = "content_loop"
WEEKS = "content_weeks"


class PlanStore(Protocol):
    def load(self, user_id: str, week_start: date) -> WeekPlan | None: ...
    def save_slot(self, user_id: str, week_start: date, slot: Slot) -> None: ...
    def save_week(self, user_id: str, plan: WeekPlan) -> None: ...


def slot_to_row(user_id: str, week_start: date, slot: Slot) -> dict[str, Any]:
    return {
        "user_id": user_id,
        "week_start": week_start.isoformat(),
        "day": slot.day,
        "slot_date": slot.slot_date.isoformat(),
        "time_label": slot.time_label,
        "state": str(slot.state),
        "optional": slot.optional,
        "drafts": [{"index": d.index, "body": d.body, "note": d.note} for d in slot.drafts],
        "published_at": slot.published_at,
        # `str(None)` is the string "None", which would sail through a text
        # column and read back as a skip reason nobody chose.
        "skip_reason": str(slot.skip_reason) if slot.skip_reason else None,
        "skip_note": slot.skip_note,
    }


def slot_from_row(row: dict[str, Any]) -> Slot:
    raw_drafts = row.get("drafts")
    drafts = tuple(
        Draft(
            index=int(
                d.get("index") or 0
            ),  # falsy-ok: draft index 0 is the first draft; a missing index means the first
            body=str(d.get("body") or ""),
            note=d.get("note"),
        )
        for d in (raw_drafts if isinstance(raw_drafts, list) else [])
        if isinstance(d, dict)
    )
    raw_reason = row.get("skip_reason")
    return Slot(
        day=str(row.get("day") or ""),
        slot_date=date.fromisoformat(str(row["slot_date"])),
        time_label=str(row.get("time_label") or "09:00"),
        state=SlotState(str(row.get("state") or "pending")),
        optional=bool(row.get("optional")),
        drafts=drafts,
        published_at=row.get("published_at"),
        skip_reason=SkipReason(raw_reason) if raw_reason else None,
        skip_note=row.get("skip_note"),
    )


def week_to_row(user_id: str, plan: WeekPlan) -> dict[str, Any]:
    return {
        "user_id": user_id,
        "week_start": plan.week_start.isoformat(),
        "cadence_per_week": plan.cadence_per_week,
        "cadence_days": list(plan.cadence_days),
        "streak_weeks": plan.streak_weeks,
        "paused_until": plan.paused_until.isoformat() if plan.paused_until else None,
        # NOT `paused_because`. V2_011 gave `content_weeks` that column and
        # `WeekPlan` has no such field — the pause REASON is derived at render
        # time on `StreakState`, from the skip reason of the slot that caused
        # it. So the column is mine and unnecessary, and writing a value into
        # it would create a second source of truth for something already
        # computed. Left unwritten rather than filled; recorded here so the
        # next reader does not assume it means something.
    }


class SupabasePlanStore:
    """Reads and writes through the CALLER's client, so RLS applies."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def load(self, user_id: str, week_start: date) -> WeekPlan | None:
        """The week, or None when it has never been planned.

        `None`, not an empty plan: "no week yet" and "a week with no slots" are
        different, and the caller decides whether to seed a default. Returning
        a default here would make an unplanned week indistinguishable from one
        the user emptied.
        """
        slot_res = (
            self._client.table(SLOTS)
            .select("*")
            .eq("user_id", user_id)
            .eq("week_start", week_start.isoformat())
            .execute()
        )
        slot_rows = slot_res.data if slot_res and isinstance(slot_res.data, list) else []

        week_res = (
            self._client.table(WEEKS)
            .select("*")
            .eq("user_id", user_id)
            .eq("week_start", week_start.isoformat())
            .execute()
        )
        week_rows = week_res.data if week_res and isinstance(week_res.data, list) else []
        week = week_rows[0] if week_rows and isinstance(week_rows[0], dict) else {}

        # The WEEK row is what says this week exists — NOT the slots.
        #
        # A paused week has cadence 0 and therefore zero slots, legitimately.
        # Keying existence off the slot rows made "you paused this week" and
        # "you have never planned a week" the same answer, so a paused user
        # would be handed a fresh default plan and lose the pause.
        #
        # Found by porting: the in-memory version stored the `WeekPlan` object
        # whole and never had to decide what an absence meant.
        if not slot_rows and not week:
            return None

        slots = tuple(slot_from_row(row) for row in slot_rows if isinstance(row, dict))
        # Chronological, because the surface renders a week in order and the
        # database returns rows in whatever order it likes.
        slots = tuple(sorted(slots, key=lambda s: s.slot_date))

        defaults = default_week(week_start)
        cadence_days = week.get("cadence_days")
        paused_until = week.get("paused_until")
        return WeekPlan(
            week_start=week_start,
            slots=slots,
            # `or` would be wrong here. A PAUSED week has cadence 0, which is
            # falsy, so `week.get(...) or default` silently promoted every
            # paused week back to two posts — the pause is stored correctly and
            # unread. The same falsy-zero trap as a credit balance of 0 and a
            # match score of 0, written fresh into a new file.
            cadence_per_week=(
                int(raw_cadence)
                if (raw_cadence := week.get("cadence_per_week")) is not None
                else defaults.cadence_per_week
            ),
            cadence_days=(
                tuple(str(d) for d in cadence_days)
                if isinstance(cadence_days, list) and cadence_days
                else defaults.cadence_days
            ),
            streak_weeks=int(
                week.get("streak_weeks") or 0
            ),  # 0 IS the default here  # falsy-ok: no streak recorded IS a streak of zero
            paused_until=date.fromisoformat(str(paused_until)) if paused_until else None,
        )

    def save_slot(self, user_id: str, week_start: date, slot: Slot) -> None:
        """ONE slot. The point of the schema.

        Writing the whole week here would reintroduce the read-modify-write
        this table shape exists to avoid — two tabs, and the later write erases
        the earlier one with no error.
        """
        self._client.table(SLOTS).upsert(
            slot_to_row(user_id, week_start, slot),
            on_conflict="user_id,week_start,day",
        ).execute()

    def save_week(self, user_id: str, plan: WeekPlan) -> None:
        """The week's own facts, and every slot it carries.

        Used when a week is first planned. Per-slot edits go through
        `save_slot`.
        """
        self._client.table(WEEKS).upsert(
            week_to_row(user_id, plan), on_conflict="user_id,week_start"
        ).execute()
        for slot in plan.slots:
            self.save_slot(user_id, plan.week_start, slot)


__all__ = [
    "SLOTS",
    "WEEKS",
    "PlanStore",
    "SupabasePlanStore",
    "slot_from_row",
    "slot_to_row",
    "week_to_row",
]
