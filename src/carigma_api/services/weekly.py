"""The weekly contract — the ritual that renews the relationship.

**NEW in V2.** Sunday evening the product proposes next week; the user signs it.
Everything Today does the following week is something the user agreed to here.

The load-bearing distinction in this module is `approved` vs `auto_adopted`,
and it is a DATA-honesty rule before it is a UI one. Skip one Sunday and a
conservative standing plan adopts itself so the product keeps working. If that
were recorded as `approved`, our own database would claim the user agreed to
something they never saw — and every downstream reader (the review, analytics,
a future "you approved this" receipt) would repeat the claim. So the states are
distinct at the DB level (a check constraint), at the type level (`ContractState`),
and behind one predicate, `user_actually_agreed`, that only `approved` satisfies.

The lapsed grammar comes from Design Brief 2 (Part A.A) and is enforced by
tests, not just documented:

- **Facts only, zero guilt.** Banned strings: "we missed you", "don't lose your
  progress", "you're falling behind".
- **Decay is weather, not judgment.** A falling sub-score is reported as a
  thing that happened, with its cause, never as a failing.
- **The streak is honest.** It broke — we name the old one and start a new one
  rather than quietly preserving it.
- **Exit ramp from the second lapse.** "Switch to fortnightly" — a smaller true
  cadence is a relationship; a weekly pretence is churn with extra steps.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any

# ── The state machine ──────────────────────────────────────────────────────


class ContractState(StrEnum):
    """Mirrors `weekly_contracts_state_check` in V2_001. Keep them in step.

    `expired` and `lapsed` used to be here and **nothing could ever write
    them**. `consecutive_lapses` counted three states of which two were dead, so it
    could only ever return what `auto_adopted` gave it — and `auto_adopt()`
    had no caller either, so the answer was always zero and the entire lapse
    escalation was unreachable.

    They are gone rather than ported. A state machine carried into a database
    with unreachable states puts values in a check constraint that nothing can
    produce, and invites the next reader to handle cases that cannot happen.
    """

    PROPOSED = "proposed"
    APPROVED = "approved"
    #: Sunday passed unanswered and the conservative plan continued. In force,
    #: NOT agreed — the distinction this module exists to keep.
    AUTO_ADOPTED = "auto_adopted"
    DECLINED = "declined"

    @property
    def is_decided(self) -> bool:
        return self is not ContractState.PROPOSED

    @property
    def user_actually_agreed(self) -> bool:
        """The ONLY place "did the user agree?" is answered.

        `auto_adopted` deliberately returns False. A plan can be in force
        without having been agreed to, and conflating the two would let the
        product tell the user they approved something they never saw.
        """
        return self is ContractState.APPROVED


class Cadence(StrEnum):
    WEEKLY = "weekly"
    FORTNIGHTLY = "fortnightly"


# Design Brief 2's TONE LAW, enforced rather than remembered. Matched
# case-insensitively against any copy this module emits.
BANNED_PHRASES: tuple[str, ...] = (
    "we missed you",
    "don't lose your progress",
    "dont lose your progress",
    "you're falling behind",
    "youre falling behind",
    "falling behind",
    "keep your streak alive",
    "you lost your streak",
)

_BANNED = re.compile("|".join(re.escape(p) for p in BANNED_PHRASES), re.IGNORECASE)


class GuiltyCopy(ValueError):
    """Raised when re-entry copy breaks the tone law. Loud on purpose."""


def assert_no_guilt(*fragments: str | None) -> None:
    """Fail fast rather than ship a shaming string.

    A lint rule would only cover literals in this file; copy assembled at
    runtime (a streak sentence, a lapse count) needs checking where it is
    built.
    """
    for fragment in fragments:
        if fragment and (hit := _BANNED.search(fragment)):
            raise GuiltyCopy(
                f"lapsed copy must state facts without guilt — found {hit.group(0)!r} "
                f"in {fragment!r}"
            )


# ── The contract itself ────────────────────────────────────────────────────


@dataclass(frozen=True)
class ContractItem:
    """One row of the week. Editable before signing, honoured on Today."""

    kind: str  # content | jobs | naukri | profile | review
    day: str  # MON … SUN
    summary: str
    minutes: int
    cost_credits: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "day": self.day,
            "summary": self.summary,
            "minutes": self.minutes,
            "cost_credits": self.cost_credits,
        }


@dataclass(frozen=True)
class WeeklyContract:
    week_start: date
    state: ContractState = ContractState.PROPOSED
    items: tuple[ContractItem, ...] = ()
    proposed_at: datetime | None = None
    decided_at: datetime | None = None
    cadence: Cadence = Cadence.WEEKLY
    # Which item kinds the user accepted. Partial approval is allowed: they may
    # take the content plan and skip the scans.
    accepted_kinds: tuple[str, ...] = ()

    @property
    def total_minutes(self) -> int:
        return sum(i.minutes for i in self.items)

    @property
    def total_credits(self) -> int:
        return sum(i.cost_credits for i in self.items)

    @property
    def in_force_items(self) -> tuple[ContractItem, ...]:
        """What Today may actually act on this week.

        Nothing is in force until the contract is decided, and after a partial
        approval only the accepted kinds are — Today must never run something
        the user declined.
        """
        if self.state is ContractState.APPROVED:
            return tuple(i for i in self.items if i.kind in self.accepted_kinds)
        if self.state is ContractState.AUTO_ADOPTED:
            return self.items
        return ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "week_start": self.week_start.isoformat(),
            "state": str(self.state),
            "cadence": str(self.cadence),
            # Surfaced so no client has to re-derive the distinction — and so
            # a client that forgets to cannot accidentally claim agreement.
            "user_approved": self.state.user_actually_agreed,
            "items": [i.as_dict() for i in self.items],
            "accepted_kinds": list(self.accepted_kinds),
            "total_minutes": self.total_minutes,
            "total_credits": self.total_credits,
            "proposed_at": _iso(self.proposed_at),
            "decided_at": _iso(self.decided_at),
        }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


class ContractTransitionError(ValueError):
    """An illegal move on the state machine."""


def approve(
    contract: WeeklyContract,
    *,
    accept: tuple[str, ...] | None = None,
    now: datetime | None = None,
) -> WeeklyContract:
    """The signature. Only a real user action reaches this function."""
    if contract.state.is_decided:
        raise ContractTransitionError(f"week of {contract.week_start} is already {contract.state}")

    kinds = tuple(i.kind for i in contract.items)
    chosen = tuple(k for k in kinds if accept is None or k in accept)
    if unknown := set(accept or ()) - set(kinds):
        raise ContractTransitionError(f"not in this contract: {sorted(unknown)}")
    # Accepting nothing is a decline, and is recorded as one. Recording it as
    # an approval of an empty plan would misstate what happened.
    if not chosen:
        return decline(contract, now=now)

    return _replace(
        contract,
        state=ContractState.APPROVED,
        accepted_kinds=chosen,
        decided_at=now or datetime.now(UTC),
    )


def decline(contract: WeeklyContract, *, now: datetime | None = None) -> WeeklyContract:
    if contract.state.is_decided:
        raise ContractTransitionError(f"week of {contract.week_start} is already {contract.state}")
    return _replace(
        contract,
        state=ContractState.DECLINED,
        accepted_kinds=(),
        decided_at=now or datetime.now(UTC),
    )


def auto_adopt(contract: WeeklyContract, *, now: datetime | None = None) -> WeeklyContract:
    """Sunday passed unanswered — the conservative plan takes effect.

    NOT an approval, and `accepted_kinds` stays empty because the user accepted
    nothing. The plan is in force (see `in_force_items`) without being agreed
    to, which is exactly the distinction this module exists to keep.
    """
    if contract.state.is_decided:
        raise ContractTransitionError(f"week of {contract.week_start} is already {contract.state}")
    return _replace(
        contract,
        state=ContractState.AUTO_ADOPTED,
        items=conservative_items(contract.items),
        accepted_kinds=(),
        decided_at=now or datetime.now(UTC),
    )


def _replace(contract: WeeklyContract, **changes: Any) -> WeeklyContract:
    from dataclasses import replace

    return replace(contract, **changes)


# ── The conservative standing plan ─────────────────────────────────────────

# Brief 2 MECHANICS: "scans and decay tracking continue, new post drafts pause
# (nothing piles up)." Drafting posts nobody asked for would build a backlog
# the user returns to as a debt — the opposite of the intent.
CONTINUES_UNATTENDED: frozenset[str] = frozenset({"jobs", "profile", "naukri", "review"})
PAUSES_UNATTENDED: frozenset[str] = frozenset({"content"})


def conservative_items(items: tuple[ContractItem, ...]) -> tuple[ContractItem, ...]:
    """Keep what runs safely without a person; drop what would pile up."""
    return tuple(i for i in items if i.kind in CONTINUES_UNATTENDED)


# ── Lapse counting ─────────────────────────────────────────────────────────


def consecutive_lapses(history: list[WeeklyContract], *, upto: date) -> int:
    """How many Sundays in a row went unanswered, most recent first.

    Counts backwards from the most recent week and stops at the first week the
    user actually engaged with. A decline breaks the run: declining is an
    answer, and treating it as a lapse would call an active choice neglect.
    """
    past = sorted(
        (c for c in history if c.week_start <= upto),
        key=lambda c: c.week_start,
        reverse=True,
    )
    lapses = 0
    for contract in past:
        # One state, because one state is what exists. This read
        # `(AUTO_ADOPTED, EXPIRED, LAPSED)` while two of the three were
        # unwritable — a list that looked thorough and was a single term.
        if contract.state is ContractState.AUTO_ADOPTED:
            lapses += 1
            continue
        break
    return lapses


def presentation_for(lapses: int) -> str:
    """How the lapse is shown. Escalation is deliberate and bounded.

    One skip is unremarkable, so it gets a banner, not a takeover. Two is a
    pattern worth naming, so Today becomes the standing-by card.
    """
    if lapses <= 0:
        return "none"
    if lapses == 1:
        return "banner"
    return "standing_by"


def offers_fortnightly(lapses: int) -> bool:
    """The exit ramp, offered from the SECOND lapse (Brief 2 EXIT RAMP)."""
    return lapses >= 2
