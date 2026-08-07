"""The content practice — week plan, slots, and the streak.

Brief 3a §04: *"The practice. Paper inside the instrument."*

The rule this module exists to protect is in the verb. **We never auto-post.**
Carigma drafts; the user copies, publishes on LinkedIn themselves, and comes
back to say so. "Mark published" is therefore the honest verb, and the streak
counts *what they shipped, not what we generated*. Any future shortcut that
increments the streak on our side of that line is the same class of lie as
inventing a salary — it claims a real-world event we did not witness.

Two consequences fall out of that and are enforced here:

- `mark_published` is the ONLY transition that advances the streak. Generating,
  regenerating, copying and reviewing all leave it untouched.
- **A skip pauses the streak; it never resets it.** Brief 3a is explicit. A
  fortnight of travel is not a failure, and zeroing someone's six weeks for it
  would be the product punishing a life event.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from enum import StrEnum
from typing import Any

from carigma_api.services.weekly import assert_no_guilt


class SlotState(StrEnum):
    #: Content Intelligence has not run for this slot yet.
    PENDING = "pending"
    #: Drafts exist and are waiting for a human.
    DRAFT_READY = "draft_ready"
    #: The user said they published it. The only state that counts.
    PUBLISHED = "published"
    #: Declined, with a reason we learn from.
    SKIPPED = "skipped"


class SkipReason(StrEnum):
    """Declining teaches. Two of these four retrain the agent; two don't.

    "Travelling" says nothing about the writing, so treating it as negative
    feedback on the drafts would teach the model the wrong lesson from a
    perfectly good week.
    """

    NO_TIME = "no_time"
    NOT_MY_TOPIC = "not_my_topic"
    DRAFTS_WEAK = "drafts_weak"
    OTHER = "other"

    @property
    def retrains(self) -> bool:
        return self in (SkipReason.NOT_MY_TOPIC, SkipReason.DRAFTS_WEAK)

    @property
    def label(self) -> str:
        return {
            "no_time": "Travelling / no time this week",
            "not_my_topic": "Topic doesn't feel like me",
            "drafts_weak": "Drafts weren't good enough",
            "other": "Other",
        }[self.value]


@dataclass(frozen=True)
class Draft:
    """One variant. Never presented as finished — the user edits on paper."""

    index: int
    body: str
    note: str | None = None  # e.g. "stronger hook"

    @property
    def word_count(self) -> int:
        return len(self.body.split())

    @property
    def reading_seconds(self) -> int:
        # 240 wpm — a LinkedIn post is skimmed, not studied. Rounded to :05 so
        # it reads as an estimate rather than a false precision.
        seconds = round(self.word_count / 240 * 60)
        return max(5, round(seconds / 5) * 5)

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "body": self.body,
            "note": self.note,
            "word_count": self.word_count,
            "reading_seconds": self.reading_seconds,
        }


@dataclass(frozen=True)
class Slot:
    """A day the user agreed to post on.

    `optional` slots are the one-off extras. Brief 3a: *"Skipping an optional
    slot is free — no reason asked."* Asking someone to justify not doing
    something they never committed to is how a tool starts to nag.
    """

    day: str  # MON … SUN
    slot_date: date
    time_label: str = "09:00"
    state: SlotState = SlotState.PENDING
    optional: bool = False
    drafts: tuple[Draft, ...] = ()
    published_at: str | None = None
    skip_reason: SkipReason | None = None
    skip_note: str | None = None

    @property
    def counts_toward_streak(self) -> bool:
        """Only a real, user-confirmed publish does."""
        return self.state is SlotState.PUBLISHED

    def as_dict(self) -> dict[str, Any]:
        return {
            "day": self.day,
            "date": self.slot_date.isoformat(),
            "time_label": self.time_label,
            "state": str(self.state),
            "optional": self.optional,
            "drafts": [d.as_dict() for d in self.drafts],
            "variant_count": len(self.drafts),
            "published_at": self.published_at,
            "skip_reason": str(self.skip_reason) if self.skip_reason else None,
            "skip_note": self.skip_note,
        }


class SlotTransitionError(ValueError):
    """An illegal move on a slot."""


def mark_published(slot: Slot, *, at: str) -> Slot:
    """The honest verb. The user is telling us they shipped it.

    Deliberately NOT called `publish` — this function posts nothing anywhere.
    It records a claim the user made about the real world, which is the only
    thing we are in a position to know.
    """
    if slot.state is SlotState.PUBLISHED:
        raise SlotTransitionError(f"{slot.day} is already marked published")
    if slot.state is SlotState.SKIPPED:
        raise SlotTransitionError(f"{slot.day} was skipped")
    return replace(slot, state=SlotState.PUBLISHED, published_at=at)


def skip(slot: Slot, *, reason: SkipReason | None = None, note: str | None = None) -> Slot:
    """Declining is free, and it teaches.

    An optional slot needs no reason. A committed slot does — not as a toll,
    but because "the drafts weren't good enough" and "I was travelling" are
    different facts and the agent must not confuse them.

    `note` is the USER's own words and is stored verbatim. The tone law governs
    copy Carigma writes; it is not a filter on what someone may say about their
    own week. Refusing to record "I'm falling behind" because our style guide
    dislikes the phrase would be the product censoring the user to protect its
    own voice.
    """
    if slot.state is SlotState.PUBLISHED:
        raise SlotTransitionError(f"{slot.day} is already published")
    if not slot.optional and reason is None:
        raise SlotTransitionError("a committed slot needs a reason — it is what teaches")
    return replace(slot, state=SlotState.SKIPPED, skip_reason=reason, skip_note=note)


@dataclass(frozen=True)
class WeekPlan:
    week_start: date
    slots: tuple[Slot, ...] = ()
    cadence_per_week: int = 2
    cadence_days: tuple[str, ...] = ("MON", "THU")
    #: Weeks of unbroken shipping carried into this week.
    streak_weeks: int = 0
    #: Set when the user has deliberately paused (cadence 0).
    paused_until: date | None = None

    @property
    def shipped(self) -> int:
        return sum(1 for s in self.slots if s.counts_toward_streak)

    @property
    def committed(self) -> int:
        return sum(1 for s in self.slots if not s.optional)

    @property
    def is_paused(self) -> bool:
        return self.cadence_per_week == 0 or self.paused_until is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "week_start": self.week_start.isoformat(),
            "slots": [s.as_dict() for s in self.slots],
            "cadence_per_week": self.cadence_per_week,
            "cadence_days": list(self.cadence_days),
            "cadence_label": _cadence_label(self.cadence_per_week, self.cadence_days),
            "shipped": self.shipped,
            "committed": self.committed,
            "streak": streak_state(self).as_dict(),
            "paused_until": self.paused_until.isoformat() if self.paused_until else None,
        }


def _cadence_label(per_week: int, days: tuple[str, ...]) -> str:
    if per_week == 0:
        return "Paused"
    return f"{per_week}/wk · {' + '.join(d.title() for d in days)}"


# ── The streak ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class StreakState:
    weeks: int
    paused: bool = False
    #: Why it is paused, in the user's own terms. Never a judgment.
    paused_because: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def sentence(self) -> str:
        if self.paused:
            # "Held, not lost." Brief 3a is explicit that the number survives.
            return f"Paused at {self.weeks} weeks"
        if self.weeks == 0:
            return "No streak yet. It starts with the first post you ship."
        return f"Streak: {self.weeks} weeks"

    def as_dict(self) -> dict[str, Any]:
        sentence = self.sentence()
        assert_no_guilt(sentence, self.paused_because)
        return {
            "weeks": self.weeks,
            "paused": self.paused,
            "paused_because": self.paused_because,
            "sentence": sentence,
        }


def streak_state(plan: WeekPlan) -> StreakState:
    """A skip PAUSES the streak. It never resets it.

    This is the rule most likely to be "simplified" later by someone
    reimplementing the counter, so it is one function with one test suite.
    """
    if plan.is_paused:
        return StreakState(plan.streak_weeks, paused=True, paused_because="cadence set to 0")

    skipped = [s for s in plan.slots if s.state is SlotState.SKIPPED and not s.optional]
    if skipped and plan.shipped == 0:
        return StreakState(
            plan.streak_weeks,
            paused=True,
            paused_because=skipped[0].skip_reason.label if skipped[0].skip_reason else None,
        )

    # A week where every committed slot shipped advances it. A partial week
    # holds — neither rewarded nor punished.
    if plan.committed and plan.shipped >= plan.committed:
        return StreakState(plan.streak_weeks + 1)
    return StreakState(plan.streak_weeks)


# ── The review checklist ───────────────────────────────────────────────────


@dataclass(frozen=True)
class ReviewNote:
    """What the agent checked. `ok=False` is a suggestion, not a failure.

    Brief 3a shows "▲ Ending is soft — consider a question" beside three ticks.
    The register is advisory throughout: the user's judgment beats ours, and a
    checklist that scolds would make them defend their own writing to a tool.
    """

    ok: bool
    text: str

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "glyph": "✓" if self.ok else "▲", "text": self.text}


def default_week(start: date, days: tuple[str, ...] = ("MON", "THU")) -> WeekPlan:
    """A week's committed slots, plus the optional Saturday one-off."""
    index = {"MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6}
    slots = [
        Slot(day=day, slot_date=start + timedelta(days=index[day])) for day in days if day in index
    ]
    slots.append(Slot(day="SAT", slot_date=start + timedelta(days=5), optional=True))
    return WeekPlan(
        week_start=start, slots=tuple(slots), cadence_days=days, cadence_per_week=len(days)
    )
