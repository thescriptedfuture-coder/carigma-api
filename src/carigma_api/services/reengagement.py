"""The lapsed-user digest: a decaying cadence with a real ending.

## Why decay rather than sustained weekly

The design system's own lapsed grammar argues against it: *"a smaller true
cadence is a relationship; a weekly pretence is churn with extra steps."* So
the structure is kept and the interval widens — roughly eight touchpoints over
a longer window, each still carrying fresh market content, with materially less
unsubscribe risk than eight weekly ones.

    phase 1   weekly        x3
    phase 2   fortnightly   x3
    phase 3   monthly       x2
    then      STOP, permanently

## "Stop permanently" is written, not derived

The tempting implementation counts sends and compares against `CADENCE`. That
is a comparison against a table that can change: add a fourth weekly phase and
everyone who finished at eight is silently below the new total, resurrected
months later by a config edit nobody connected to them.

So `State.FINISHED` is written at the moment the last send completes and never
recomputed. `due_sequences()` only ever looks at `active` rows, so no edit to
`CADENCE` can reopen a closed one. A finished sequence is a fact about what
happened; a count is an opinion about a table.

## The content rule

Market-facing, never user-shaming. The market read is true whether or not they
opened the app — that is precisely what makes it the one honest thing to send
someone who did not. Everything the lapsed grammar bans is banned here, and
`assert_no_guilt` (shared with the rest of the mail path) is applied before
anything can leave.

**No curated research this week means no send.** `market.gist_for_email`
returns `None` rather than `[]` for that case, so this module cannot
accidentally build an email with an empty section.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from carigma_api.services.market import MarketItem

# Both from `weekly`, which is where the lapsed grammar lives. `emails`
# re-exports assert_no_guilt but does not declare it in __all__, and
# importing through a module that does not own a name is how a rename
# breaks a file that never mentioned the original.
from carigma_api.services.weekly import GuiltyCopy, assert_no_guilt

#: A user is lapsed once they have not signed in for this long.
LAPSE_AFTER = timedelta(days=7)


class State(StrEnum):
    ACTIVE = "active"
    #: They signed in. Closed, kept as history.
    RETURNED = "returned"
    #: All sends made, never came back. Terminal, permanently.
    FINISHED = "finished"


@dataclass(frozen=True)
class Phase:
    label: str
    interval: timedelta
    sends: int


#: The cadence. Data, so a test can assert the shape directly — and so the
#: comment above about NOT deriving terminality from it has something concrete
#: to point at.
CADENCE: tuple[Phase, ...] = (
    Phase("weekly", timedelta(days=7), 3),
    Phase("fortnightly", timedelta(days=14), 3),
    Phase("monthly", timedelta(days=30), 2),
)

TOTAL_SENDS = sum(p.sends for p in CADENCE)


def phase_for(sends_made: int) -> Phase | None:
    """Which phase the NEXT send belongs to, or None when the sequence is done.

    Used to schedule, never to decide whether someone is finished — that is
    `State.FINISHED`, written once.
    """
    remaining = sends_made
    for phase in CADENCE:
        if remaining < phase.sends:
            return phase
        remaining -= phase.sends
    return None


@dataclass
class Sequence:
    """One lapse-and-maybe-return episode."""

    id: int
    user_id: str
    sends_made: int = 0
    state: State = State.ACTIVE
    next_due_at: datetime | None = None
    last_sent_at: datetime | None = None
    closed_at: datetime | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Sequence:
        return cls(
            id=int(row["id"]),
            user_id=str(row["user_id"]),
            sends_made=int(row.get("sends_made") or 0),
            state=State(row.get("state") or "active"),
            next_due_at=_parse(row.get("next_due_at")),
            last_sent_at=_parse(row.get("last_sent_at")),
            closed_at=_parse(row.get("closed_at")),
        )

    @property
    def is_open(self) -> bool:
        return self.state is State.ACTIVE

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "sends_made": self.sends_made,
            "state": str(self.state),
            "next_due_at": _iso(self.next_due_at),
            "last_sent_at": _iso(self.last_sent_at),
            "closed_at": _iso(self.closed_at),
        }


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def has_lapsed(last_sign_in: datetime | None, now: datetime) -> bool:
    """Seven days without a sign-in.

    A user who has NEVER signed in is not lapsed — they are new, and belong to
    onboarding rather than to a re-engagement sequence. Treating a missing
    timestamp as an ancient one would put someone into a lapsed flow on the day
    they registered.
    """
    if last_sign_in is None:
        return False
    return now - last_sign_in >= LAPSE_AFTER


def start(user_id: str, now: datetime) -> dict[str, Any]:
    """The row for a newly lapsed user. Phase 1, due immediately."""
    return {
        "user_id": user_id,
        "started_at": now.isoformat(),
        "sends_made": 0,
        "next_due_at": now.isoformat(),
        "state": str(State.ACTIVE),
    }


def record_send(sequence: Sequence, now: datetime) -> dict[str, Any]:
    """Advance a sequence after a digest actually went out.

    Returns the update to persist. When this was the LAST send, the row is
    closed as `finished` here and now — not left to be inferred later by
    something comparing `sends_made` against `CADENCE`.
    """
    sends = sequence.sends_made + 1
    next_phase = phase_for(sends)

    if next_phase is None:
        return {
            "sends_made": sends,
            "last_sent_at": now.isoformat(),
            "next_due_at": None,
            # Written, once, at the moment it becomes true.
            "state": str(State.FINISHED),
            "closed_at": now.isoformat(),
        }

    return {
        "sends_made": sends,
        "last_sent_at": now.isoformat(),
        "next_due_at": (now + next_phase.interval).isoformat(),
        "state": str(State.ACTIVE),
    }


def close_on_return(now: datetime) -> dict[str, Any]:
    """They signed in. Stop, and keep the row as history.

    Not deleted: the lapse-and-return episode is the interesting datum, and
    admin surfaces it. A new lapse opens a NEW sequence at phase 1 rather than
    resetting a counter — which is what makes "restarts only if they lapse
    again" true by construction.
    """
    return {"state": str(State.RETURNED), "closed_at": now.isoformat()}


def is_due(sequence: Sequence, now: datetime) -> bool:
    """Only an ACTIVE sequence with a due date in the past.

    The state check is first and is not redundant: it is the thing that makes a
    finished sequence unreachable no matter what `CADENCE` says.
    """
    if sequence.state is not State.ACTIVE:
        return False
    return sequence.next_due_at is not None and now >= sequence.next_due_at


def due_sequences(sequences: list[Sequence], now: datetime) -> list[Sequence]:
    return [s for s in sequences if is_due(s, now)]


# ── The email ──────────────────────────────────────────────────────────────

#: Phrases the lapsed grammar forbids outright. Checked on the assembled body,
#: because an email is the easiest place to shame someone into unsubscribing
#: permanently — and an unsubscribe is not a retry, it is the channel gone.
BANNED = (
    "we miss you",
    "don't lose",
    "do not lose",
    "falling behind",
    "you're behind",
    "you are behind",
    "haven't been back",
    "inactive",
    "still interested",
    "come back",
)


class WouldShame(GuiltyCopy):
    """Refused before it can reach an inbox.

    A SUBCLASS of `GuiltyCopy` rather than a sibling. The shared lapsed-grammar
    guard already rejects "falling behind" and "don't lose"; this adds the
    phrases specific to re-engagement. Two unrelated exception types for one
    concern would make every caller catch both, and the one that forgot would
    ship the string.
    """


def assert_market_facing(subject: str, body: str) -> None:
    """The lapsed grammar, enforced rather than reviewed.

    Complements `emails.assert_no_guilt` (which governs all Carigma copy) with
    the phrases specific to re-engagement, where the pull toward "we miss you"
    is strongest.
    """
    assert_no_guilt(subject, body)
    haystack = f"{subject}\n{body}".lower()
    hits = [phrase for phrase in BANNED if phrase in haystack]
    if hits:
        raise WouldShame(
            f"Re-engagement copy must be market-facing, never about the user's absence. "
            f"Found: {', '.join(hits)}"
        )


@dataclass(frozen=True)
class Digest:
    subject: str
    body: str


def build_digest(
    items: list[MarketItem] | None,
    *,
    missing_skills: tuple[str, ...] = (),
) -> Digest | None:
    """The lapsed digest, or None when there is nothing true to send.

    `items is None` means no curated research this week. Returning None rather
    than an empty digest is the whole point: a stale or hollow send is the
    manufactured contact this feature exists to refuse.

    `missing_skills` is the one relevant-to-them line, framed as market
    information — "three of this week's trending skills aren't on your profile"
    — never as decay or neglect.
    """
    if not items:
        return None

    lines = [
        "What moved in the market this week:",
        "",
        *[f"· {item.headline} ({item.source_name})" for item in items],
    ]

    if missing_skills:
        count = len(missing_skills)
        lines += [
            "",
            # Market information about the market, which happens to intersect
            # their profile. Not a report on what they failed to do.
            f"{count} of this week's trending skills "
            f"{'is' if count == 1 else 'are'} not on your profile yet: "
            f"{', '.join(missing_skills)}.",
        ]

    lines += ["", "Every claim above links to its source in the app."]

    digest = Digest(subject="What the market did this week", body="\n".join(lines))
    # The gate runs on the assembled text, so no combination of parts can slip
    # through by being individually innocent.
    assert_market_facing(digest.subject, digest.body)
    return digest


__all__ = [
    "BANNED",
    "CADENCE",
    "LAPSE_AFTER",
    "TOTAL_SENDS",
    "Digest",
    "Phase",
    "Sequence",
    "State",
    "WouldShame",
    "assert_market_facing",
    "build_digest",
    "close_on_return",
    "due_sequences",
    "has_lapsed",
    "is_due",
    "phase_for",
    "record_send",
    "start",
]
