"""The Sunday review — the record, before the contract.

Design Brief 3a: *"The record, then the contract. Approving next week is a
signature, not a click."* This module builds the record half.

Two rules are enforced structurally rather than trusted to reviewers:

1. **Every fact carries a receipt.** `ReviewFact.receipt` is a required
   positional field, so a claim with no provenance cannot be constructed. This
   is the same move as `<EmptyHonest>`'s required props and the quiet-gold G2
   rule — if the type system can hold the standard, it should.

2. **Decline is data, not sin.** A falling score sits in the same list, in the
   same shape, as a rising one. Brief 3a labels the decay row "DATA, NOT SIN";
   here that means a `DOWN` fact is built by the same constructor as an `UP`
   one and carries a cause, so it reads as weather rather than judgment.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any

from carigma_api.services.weekly import (
    Cadence,
    ContractItem,
    WeeklyContract,
    assert_no_guilt,
    offers_fortnightly,
    presentation_for,
)


class Direction(StrEnum):
    """Rendered as glyph + word, never colour alone (accessibility §09)."""

    UP = "up"
    DOWN = "down"
    DONE = "done"
    NEUTRAL = "neutral"

    @property
    def glyph(self) -> str:
        return {"up": "▲", "down": "▼", "done": "✓", "neutral": "·"}[self.value]


@dataclass(frozen=True)
class ReviewFact:
    """One line of "the week that was".

    `receipt` is required. A fact the product cannot evidence does not belong
    in the record — and making it optional would mean the first rushed call
    site quietly drops it.
    """

    direction: Direction
    text: str
    receipt: str

    def __post_init__(self) -> None:
        if not self.receipt.strip():
            raise ValueError(f"fact without a receipt: {self.text!r}")
        assert_no_guilt(self.text, self.receipt)

    def as_dict(self) -> dict[str, Any]:
        return {
            "direction": str(self.direction),
            "glyph": self.direction.glyph,
            "text": self.text,
            "receipt": self.receipt,
        }


@dataclass(frozen=True)
class Streak:
    """Honest about breaking. Brief 2: "it broke — we say the old one and start
    a new one" rather than quietly preserving the number."""

    weeks: int
    previous_best: int = 0
    broken: bool = False

    def sentence(self) -> str:
        if self.broken:
            # Names the old streak as a fact and points forward, with no
            # instruction to feel anything about it.
            return f"Last streak: {self.previous_best} weeks. A new one starts when you ship."
        if self.weeks == 0:
            return "No streak yet. It starts with the first post you ship."
        return f"Streak: {self.weeks} weeks"

    def as_dict(self) -> dict[str, Any]:
        sentence = self.sentence()
        assert_no_guilt(sentence)
        return {
            "weeks": self.weeks,
            "previous_best": self.previous_best,
            "broken": self.broken,
            "sentence": sentence,
        }


# Day-of-month without a leading zero. `%-d` is POSIX-only and raises on
# Windows, where this runs in development — so the day is formatted by hand.
def _long_date(value: date) -> str:
    return f"{value:%A} {value.day} {value:%b}"


def _short_date(value: date) -> str:
    return f"{value:%a} {value.day} {value:%b}"


def standing_by_card(
    *,
    since: date,
    lapses: int,
    facts: tuple[ReviewFact, ...],
    streak: Streak,
) -> dict[str, Any]:
    """The lapsed presentation. "Standing by", not abandoned.

    Brief 2: one skip is unremarkable and gets a banner; two make Today the
    card. The copy states what the product did in the user's absence and what
    changed, then offers two doors — pick the thread back up, or move to a
    cadence that is actually true.
    """
    presentation = presentation_for(lapses)
    sundays = "Sunday" if lapses == 1 else f"{lapses} Sundays"

    if presentation == "banner":
        # One skip: say what was done, offer the review once, move on.
        title = f"{_long_date(since)} passed without a review"
        body = (
            "A conservative standing plan was adopted: scans and decay tracking "
            "continue, new drafts paused. Nothing piled up."
        )
    else:
        title = f"Standing by since {_short_date(since)}"
        body = f"{sundays} passed. Here's what actually happened."

    actions = [{"label": "Pick up the thread — plan this week", "action": "propose_re_entry"}]
    if offers_fortnightly(lapses):
        actions.append({"label": "Switch to fortnightly", "action": "set_cadence_fortnightly"})

    assert_no_guilt(title, body, *(a["label"] for a in actions))

    return {
        "presentation": presentation,
        "since": since.isoformat(),
        "lapsed_weeks": lapses,
        "title": title,
        "body": body,
        # The facts are the point: the user gets the ledger of their absence,
        # not a plea to come back.
        "facts": [f.as_dict() for f in facts],
        "streak": streak.as_dict(),
        "actions": actions,
    }


# ── Re-entry ───────────────────────────────────────────────────────────────

# Brief 3a: "Re-entry contracts are deliberately smaller — the same signature,
# a lighter promise." Coming back to a full week's ask is how people bounce off
# a second time.
RE_ENTRY_MAX_MINUTES = 25


def re_entry_contract(
    *,
    week_start: date,
    candidates: tuple[ContractItem, ...],
    cadence: Cadence = Cadence.WEEKLY,
) -> WeeklyContract:
    """A lighter week, proposed. Same signature, smaller promise."""
    # The Sunday review always survives — it is the ritual being restarted.
    review = tuple(i for i in candidates if i.kind == "review")
    rest = sorted(
        (i for i in candidates if i.kind != "review"),
        key=lambda i: i.minutes,
    )

    kept: list[ContractItem] = list(review)
    budget = RE_ENTRY_MAX_MINUTES - sum(i.minutes for i in kept)
    seen_kinds = {i.kind for i in kept}
    for item in rest:
        # One of each kind at most — variety over volume on the way back in.
        if item.kind in seen_kinds or item.minutes > budget:
            continue
        kept.append(item)
        seen_kinds.add(item.kind)
        budget -= item.minutes

    order = {"MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6}
    return WeeklyContract(
        week_start=week_start,
        items=tuple(sorted(kept, key=lambda i: order.get(i.day, 9))),
        cadence=cadence,
    )
