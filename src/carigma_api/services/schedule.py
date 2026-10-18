"""The week's posting schedule is the week the user signed. Roadmap S8.

## The decision (V1_V2_COMPARISON Part 2, 2026-10-11)

"The signed contract is the source of truth. Cadence becomes the default
proposal; the signed week is the schedule."

## What it replaced

`GET /posts/week` returned Monday and Thursday — plus an optional Saturday —
for every account, with a contract or without one: `default_week` was the
schedule and nothing read the contract. Measured on 2026-10-18: Ravi had
signed "One post slot · THU" for the week, Posts showed Monday and Thursday,
and he published on Monday — a day his signed plan did not contain. The
signature was decorative.

## The rules

- **A content row on an in-force contract is a slot.** In force means signed
  with `content` among the accepted kinds, or auto-adopted
  (`WeeklyContract.in_force_items`, the one place that answers it).
- **Work is never hidden.** A stored slot on a day the contract does not carry
  — drafted, published, skipped — stays, marked `optional`: it is real, and it
  is outside the plan. A stored slot with nothing done to it does not; it was
  only ever the old default.
- **No signed week, no slots.** Proposed, declined and absent weeks schedule
  nothing, and `Standing` says which, so the surface can say what is true and
  where to change it. The product never plans on someone's behalf and then
  acts.
- **Cadence seeds the proposal**, nothing else: `content_days` is how many
  posts a week the user chose at onboarding, as days, for `routes/weekly` to
  offer.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from enum import StrEnum
from typing import Any

from carigma_api.services.posts import Slot, SlotState, WeekPlan
from carigma_api.services.weekly import ContractState, WeeklyContract

DAY_INDEX = {"MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6}

#: Posting days per weekly cadence — V1's `_POST_DAYS` (resumeiq/today.py), so
#: a user who chose "3 a week" in either version gets the same three days.
CADENCE_DAYS: dict[int, tuple[str, ...]] = {
    2: ("TUE", "THU"),
    3: ("MON", "WED", "FRI"),
    5: ("MON", "TUE", "WED", "THU", "FRI"),
}

#: V1's fallback for an unknown cadence, and onboarding's default.
DEFAULT_CADENCE = 3


class Standing(StrEnum):
    """Where this week's schedule comes from — or why there is none."""

    #: Signed by the user.
    SIGNED = "signed"
    #: Sunday passed unanswered and the conservative plan continued. In force,
    #: NOT agreed: the distinction `services.weekly` exists to keep.
    ADOPTED = "adopted"
    #: A plan is waiting for a signature. Nothing is scheduled until it has one.
    PROPOSED = "proposed"
    #: The user declined this week.
    DECLINED = "declined"
    #: No plan exists for this week at all.
    NONE = "none"


def content_days(cadence: Any) -> tuple[str, ...]:
    """The posting days a cadence proposes. Unknown → V1's three."""
    try:
        return CADENCE_DAYS.get(int(cadence), CADENCE_DAYS[DEFAULT_CADENCE])
    except (TypeError, ValueError):
        return CADENCE_DAYS[DEFAULT_CADENCE]


def standing_of(contract: WeeklyContract | None) -> Standing:
    if contract is None:
        return Standing.NONE
    return {
        ContractState.APPROVED: Standing.SIGNED,
        ContractState.AUTO_ADOPTED: Standing.ADOPTED,
        ContractState.PROPOSED: Standing.PROPOSED,
        ContractState.DECLINED: Standing.DECLINED,
    }[contract.state]


def scheduled_days(contract: WeeklyContract | None) -> tuple[str, ...]:
    """The content days in force on `contract`, in week order, each once."""
    if contract is None:
        return ()
    days = {item.day for item in contract.in_force_items if item.kind == "content"}
    return tuple(sorted((d for d in days if d in DAY_INDEX), key=DAY_INDEX.__getitem__))


def _worked_on(slot: Slot) -> bool:
    """Whether the user has done anything with a stored slot."""
    return bool(slot.drafts) or slot.state is not SlotState.PENDING


def schedule(
    week_start: date,
    *,
    contract: WeeklyContract | None,
    stored: WeekPlan | None,
) -> WeekPlan:
    """The week as the user signed it, with every piece of their work on it."""
    days = scheduled_days(contract)
    by_day = {slot.day: slot for slot in (stored.slots if stored else ())}

    slots: list[Slot] = []
    for day in days:
        slot = by_day.pop(day, None) or Slot(
            day=day, slot_date=week_start + timedelta(days=DAY_INDEX[day])
        )
        slots.append(replace(slot, optional=False))
    # Outside the plan, and real: drafted, published or skipped by the user.
    slots.extend(replace(slot, optional=True) for slot in by_day.values() if _worked_on(slot))

    return WeekPlan(
        week_start=week_start,
        slots=tuple(sorted(slots, key=lambda s: s.slot_date)),
        cadence_per_week=len(days),
        cadence_days=days,
        streak_weeks=stored.streak_weeks if stored else 0,
        paused_until=stored.paused_until if stored else None,
        # Explicit, because zero content rows on a signed week is not a pause:
        # it is a week signed without a post in it.
        paused=bool(stored and stored.is_paused),
    )


#: What the Posts page says when the schedule has no slots, by standing. The
#: fact, then the one place to change it — never a verdict on the person.
EMPTY: dict[Standing, dict[str, str]] = {
    Standing.NONE: {
        "happened": "No week is signed yet.",
        "why": "Your posts follow the week you sign — each post on it becomes a slot here.",
        "next": "Plan this week on Progress. It proposes your usual posting days.",
    },
    Standing.PROPOSED: {
        "happened": "This week's plan is waiting for your signature.",
        "why": "Nothing is scheduled until you sign it.",
        "next": "Read it and sign it on Progress.",
    },
    Standing.DECLINED: {
        "happened": "You declined this week's plan.",
        "why": "So nothing is scheduled — by your choice.",
        "next": "A new week starts on Monday.",
    },
    Standing.SIGNED: {
        "happened": "The week you signed has no post in it.",
        "why": "Posts follow the week you sign.",
        "next": "To add one, change this week on Progress.",
    },
    Standing.ADOPTED: {
        "happened": "This week's plan has no post in it.",
        "why": "It carried over when Sunday's plan went unanswered.",
        "next": "To add one, change this week on Progress.",
    },
}

#: Where each empty state's one control lives.
EMPTY_ACTION: dict[Standing, dict[str, str] | None] = {
    Standing.NONE: {"label": "Plan this week", "route": "/progress"},
    Standing.PROPOSED: {"label": "Read and sign", "route": "/progress"},
    Standing.DECLINED: None,
    Standing.SIGNED: {"label": "Change this week", "route": "/progress"},
    Standing.ADOPTED: {"label": "Change this week", "route": "/progress"},
}


__all__ = [
    "CADENCE_DAYS",
    "DAY_INDEX",
    "DEFAULT_CADENCE",
    "EMPTY",
    "EMPTY_ACTION",
    "Standing",
    "content_days",
    "schedule",
    "scheduled_days",
    "standing_of",
]
