"""Event-triggered emails: more REASONS, never more frequency.

Five things can now earn an email. That is five reasons, and it must remain
one email — otherwise the feature is "we send five times as much", which is
how a product teaches people to unsubscribe. A manufactured email does not get
ignored; it gets unsubscribed, and then the ones that mattered never arrive
either.

## The ceiling is the database, not a rule

Every proactive email — the daily brief and all five triggers — claims the
same daily slot:

    log.claim_period(user_id, DAILY_SLOT, day)

One row per user per day, enforced by the unique index that already guards
duplicates. Whichever runs first wins the day and the rest are DUPLICATE. A
sixth trigger added next year therefore cannot increase anybody's volume,
because the constraint is not aware of how many triggers exist.

That is deliberately not "the sender remembers to check". A rule someone can
forget is one release away from five emails on a Tuesday.

## One fires, and it is the most urgent one

`choose()` returns a single trigger, in priority order, like the Today ladder:
a thing with a deadline beats a thing without, and something the user is
waiting on beats something we noticed. The losers do not queue — an email that
arrives two days late about a match found on Monday is worse than silence.

## Every fact is checked, never inferred

Each trigger carries an `applies` that reads real state, and a `build` that
renders only what that state contains. There is no trigger whose fact is "we
have not emailed in a while", because that is a reason to send, not a thing
that happened.

## Why the numbers are read defensively

Every numeric read here is `dict.get(...) or 0`, marked `falsy-ok`. That is the
fourth file where the falsy-zero guard has fired, and all four had the same
shape: **pulling a number out of an untyped `dict[str, Any]`.**

The deeper fix is typed facts rather than dicts, which would delete the
question. It is not done here because these payloads come from five different
sources whose shapes are not yet settled, and inventing five dataclasses to
describe rows that may change next month is the kind of structure that gets
abandoned half-converted. Marked and noted rather than half-fixed.

## Suppressed while lapsed

A user inside the re-engagement sequence gets that grammar and no other. Two
conversations at once reads as a system that does not know who it is talking
to, and the lapsed sequence already has its own cadence and its own ending.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from carigma_api.services.emails import Email, EmailType

#: The claim namespace shared by every proactive email. Passing this as
#: `claim_as` is what makes the ceiling structural.
DAILY_SLOT = "daily_slot"

#: A match has to be genuinely strong to be worth an interruption. Below this
#: it belongs in the feed, where the user looks when they choose to.
STRONG_MATCH = 85


@dataclass(frozen=True)
class Facts:
    """What is true about this user right now.

    Every field defaults to the absent value, so a source that failed to load
    contributes nothing rather than a zero that reads as a fact. The same
    distinction the jobs counter needed: `None` is "we could not look".
    """

    #: The single best new match since the last look, if any.
    top_match: dict[str, Any] | None = None
    #: A band crossing, with the receipt that justifies it.
    band_move: dict[str, Any] | None = None
    #: An application sitting untouched past the follow-up window.
    stale_application: dict[str, Any] | None = None
    #: A milestone reached since the last email.
    milestone: dict[str, str] | None = None
    #: An incremental profile update that finished processing.
    profile_update: dict[str, Any] | None = None
    #: True while the re-engagement sequence owns the conversation.
    lapsed: bool = False


@dataclass(frozen=True)
class Trigger:
    key: str
    #: Lower fires first. Ties are not possible — the numbers are distinct and
    #: asserted as an exact sequence in the tests, like the Today ladder.
    priority: int
    applies: Callable[[Facts], bool] = field(repr=False)
    build: Callable[[str, Facts], Email | None] = field(repr=False)
    #: Identifies WHICH fact fired, so the same crossing does not re-send if
    #: the daily slot happens to be free tomorrow.
    identity: Callable[[Facts], str] = field(repr=False)


# ── The five ───────────────────────────────────────────────────────────────


def _match_applies(facts: Facts) -> bool:
    match = facts.top_match
    # falsy-ok: an unscored match is not a strong one, and neither is a zero
    return bool(match) and int((match or {}).get("score") or 0) >= STRONG_MATCH


def _match_email(to: str, facts: Facts) -> Email | None:
    match = facts.top_match or {}
    title, company = match.get("title"), match.get("company")
    if not title or not company:
        # A match we cannot name is a match we cannot honestly announce.
        return None
    return Email(
        to=to,
        subject=f"A strong match: {title} at {company}",
        body=(
            f"{title} at {company} scored {match.get('score')} against your profile — "
            f"the closest thing we have seen since you last looked.\n\n"
            f"{match.get('why') or 'It lines up with your target role and your skills.'}\n\n"
            "Open Jobs to see it, or leave it — it will still be there."
        ),
        email_type=EmailType.EVENT,
    )


def _band_applies(facts: Facts) -> bool:
    move = facts.band_move or {}
    return bool(move.get("from")) and bool(move.get("to")) and bool(move.get("receipt"))


def _band_email(to: str, facts: Facts) -> Email | None:
    move = facts.band_move or {}
    direction = "up to" if move.get("improved") else "down to"
    return Email(
        to=to,
        subject=f"Your profile moved {direction} {move['to']}",
        body=(
            f"Your score moved from {move['from']} to {move['to']}.\n\n"
            # The receipt is required by `_band_applies`, so this is never a
            # bare claim. A band change without its evidence is a number we are
            # asking someone to trust.
            f"Why: {move['receipt']}\n\n"
            "Open Signal for the full breakdown."
        ),
        email_type=EmailType.EVENT,
    )


def _stale_applies(facts: Facts) -> bool:
    app = facts.stale_application or {}
    # falsy-ok: applied today is zero days, which is not stale either way
    return bool(app.get("company")) and int(app.get("days") or 0) > 0


def _stale_email(to: str, facts: Facts) -> Email | None:
    app = facts.stale_application or {}
    days = int(app["days"])
    return Email(
        to=to,
        subject=f"{app['company']} — {days} days, no reply",
        body=(
            f"You applied to {app.get('role') or 'a role'} at {app['company']} {days} days ago "
            "and nothing has moved.\n\n"
            # No instruction and no judgement: a follow-up is one option, and
            # so is deciding it is done.
            "A short follow-up is often what unsticks it. Closing it out is also "
            "a real answer.\n\n"
            "Open the tracker when you want to."
        ),
        email_type=EmailType.EVENT,
    )


def _milestone_applies(facts: Facts) -> bool:
    return bool((facts.milestone or {}).get("text"))


def _milestone_email(to: str, facts: Facts) -> Email | None:
    milestone = facts.milestone or {}
    return Email(
        to=to,
        subject=milestone.get("title") or "A milestone",
        body=f"{milestone['text']}\n\nNothing to do — just worth saying.",
        email_type=EmailType.EVENT,
    )


def _update_applies(facts: Facts) -> bool:
    update = facts.profile_update or {}
    # falsy-ok: no changes recorded IS nothing to approve
    return int(update.get("changes") or 0) > 0


def _update_email(to: str, facts: Facts) -> Email | None:
    update = facts.profile_update or {}
    count = int(update["changes"])
    thing = "change" if count == 1 else "changes"
    return Email(
        to=to,
        subject=f"{count} profile {thing} ready for you to approve",
        body=(
            f"We read your updated profile and found {count} {thing} worth making.\n\n"
            # Approval is the whole design of P6-1: nothing is applied without
            # it, and the email must not imply otherwise.
            "Nothing has been changed — they are waiting for your approval in Signal."
        ),
        email_type=EmailType.EVENT,
    )


#: Priority order, as data. The Today ladder's lesson: a branch tree cannot be
#: asserted as a sequence, and a sequence is exactly what needs reviewing.
TRIGGERS: tuple[Trigger, ...] = (
    # A deadline nobody set but the market did.
    Trigger(
        "strong_match",
        1,
        _match_applies,
        _match_email,
        lambda f: str((f.top_match or {}).get("id")),
    ),
    # Something the user is already waiting on an answer about.
    Trigger(
        "stale_application",
        2,
        _stale_applies,
        _stale_email,
        lambda f: str((f.stale_application or {}).get("id")),
    ),
    # A change in the thing they came here to improve.
    Trigger(
        "band_move",
        3,
        _band_applies,
        _band_email,
        lambda f: f"{(f.band_move or {}).get('from')}->{(f.band_move or {}).get('to')}",
    ),
    # Work waiting on their approval.
    Trigger(
        "profile_update",
        4,
        _update_applies,
        _update_email,
        lambda f: str((f.profile_update or {}).get("id")),
    ),
    # Good news with nothing to do. Last on purpose: it can always wait a day,
    # and everything above it cannot.
    Trigger(
        "milestone",
        5,
        _milestone_applies,
        _milestone_email,
        lambda f: str((f.milestone or {}).get("id") or (f.milestone or {}).get("text")),
    ),
)


def choose(facts: Facts) -> Trigger | None:
    """The single trigger that fires, or None.

    Returns at most one whatever is true. Five true facts produce one email,
    and the other four are simply not sent — not queued, not batched into a
    digest nobody asked for. They will still be true tomorrow if they matter.
    """
    if facts.lapsed:
        # The re-engagement sequence owns this user's inbox until it ends.
        return None
    for trigger in sorted(TRIGGERS, key=lambda t: t.priority):
        if trigger.applies(facts):
            return trigger
    return None


def slot_key(day: date) -> str:
    """The daily ceiling's period key. One per user per day, all types."""
    return day.isoformat()


__all__ = [
    "DAILY_SLOT",
    "STRONG_MATCH",
    "TRIGGERS",
    "Facts",
    "Trigger",
    "choose",
    "slot_key",
]
