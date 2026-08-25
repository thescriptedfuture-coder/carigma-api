"""Referral attribution: codes now, promotion later.

Launching without codes means the first hundred users can never be attributed
and we never learn which channel worked. Cheap now, impossible to retrofit.

## The grant takes an ACTIVATION, not a user id

`grant_for_activation` requires an `Activation` — a value only
`activation_from_profile_upload` can construct. There is no overload, no
optional flag, and no code path that reaches the grant from a registration.
The rule "50 credits on profile upload, never on signup" is therefore a
property of the type signature rather than a branch someone could add.

That is testable, and `test_referrals.py` asserts it by walking this module's
AST: nothing may call the grant except through an Activation.

## The cap is five SLOTS, arbitrated by the database

Counting rows and comparing to five is read-then-write. Under READ COMMITTED
two concurrent activations both see four and both insert, and the referrer has
six — exactly V1's double-credit shape. So a referral claims a NUMBERED slot,
`unique (referrer_id, slot)` decides the winner, and the loser retries the next
one. `check (slot between 1 and 5)` means six is not expressible at all.

No count is trusted anywhere in that sentence.

## Both sides are paid, and the ledger says which is which

The referrer gets 50, the referred user 20. Without the second the link is a
favour asked rather than a gift given. They are different ledger KINDS, not one
kind with different amounts, so "what did referrals cost us" is answerable
without parsing reason strings.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

#: The referrer's reward, on activation.
REFERRER_CREDITS = 50
#: The referred user's. Smaller, but the link has to give something.
REFERRED_CREDITS = 20

#: Five successful referrals, i.e. 250 credits — roughly 15-20 agent runs of
#: real API spend. Credits are the ONLY currency while payments are dormant,
#: so they are worth more than they look and an uncapped programme is an
#: unbudgeted bill.
MAX_REFERRALS = 5

#: Unambiguous when read aloud or copied out of a chat: no 0/o, 1/l/i.
_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
CODE_LENGTH = 8


class LedgerKind(StrEnum):
    """Distinct kinds so the two sides stay legible in the ledger."""

    REFERRER = "referral"
    REFERRED = "referral_bonus"


class State(StrEnum):
    PENDING = "pending"
    ACTIVATED = "activated"


class CapReached(ValueError):
    """All five slots are taken. Not an error — the programme working."""


class SelfReferral(ValueError):
    """Refused. The database also refuses it, so this is the polite layer."""


class AlreadyReferred(ValueError):
    """Someone can be referred exactly once, ever."""


def new_code() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(CODE_LENGTH))


@dataclass(frozen=True)
class Activation:
    """Proof that a referred user actually uploaded a profile.

    The ONLY key that opens `grant_for_activation`. Constructed by
    `activation_from_profile_upload` and nothing else — which is what makes
    "never on signup" structural rather than remembered.
    """

    referred_id: str
    at: datetime

    def __post_init__(self) -> None:
        if not self.referred_id:
            raise ValueError("an activation needs the user who activated")


def activation_from_profile_upload(referred_id: str, *, now: datetime | None = None) -> Activation:
    """The one constructor. Called from the profile-upload path, nowhere else."""
    return Activation(referred_id=referred_id, at=now or datetime.now(UTC))


@dataclass(frozen=True)
class Grant:
    """What to write. Two ledger rows and one state change, or nothing."""

    referrer_id: str
    referred_id: str
    slot: int
    referrer_credits: int = REFERRER_CREDITS
    referred_credits: int = REFERRED_CREDITS

    def ledger_rows(self, at: datetime) -> list[dict[str, Any]]:
        return [
            {
                "user_id": self.referrer_id,
                "delta": self.referrer_credits,
                "kind": str(LedgerKind.REFERRER),
                "reason": f"Referral activated ({self.referred_id[:8]})",
                "created_at": at.isoformat(),
            },
            {
                "user_id": self.referred_id,
                "delta": self.referred_credits,
                "kind": str(LedgerKind.REFERRED),
                "reason": "Welcome bonus from a referral",
                "created_at": at.isoformat(),
            },
        ]


def assert_referable(referrer_id: str, referred_id: str) -> None:
    """The checks that can be made before touching the database.

    The database enforces both of these too — `check (referrer_id <>
    referred_id)` and `unique (referred_id)`. This layer exists to return a
    sentence rather than a constraint violation, NOT to be the guarantee.
    """
    if not referrer_id or not referred_id:
        raise ValueError("both sides of a referral are required")
    if referrer_id == referred_id:
        raise SelfReferral("You can't refer yourself.")


def next_slot(taken: set[int]) -> int:
    """The lowest free slot, or CapReached.

    `taken` is a hint, not a decision: the caller inserts with this slot and the
    unique index decides. On a collision the caller adds the loser's slot to
    `taken` and asks again. The database is the arbiter; this only proposes.
    """
    for slot in range(1, MAX_REFERRALS + 1):
        if slot not in taken:
            return slot
    raise CapReached(
        f"That's {MAX_REFERRALS} successful referrals — the most one account can earn."
    )


def grant_for_activation(
    activation: Activation,
    *,
    referrer_id: str,
    slot: int,
) -> Grant:
    """The grant. Reachable ONLY with an Activation.

    Takes the activation rather than a user id so there is no signature this
    can be called with from a registration handler. The type is the guarantee.
    """
    assert_referable(referrer_id, activation.referred_id)
    if not 1 <= slot <= MAX_REFERRALS:
        raise CapReached(f"slot {slot} is outside 1..{MAX_REFERRALS}")
    return Grant(referrer_id=referrer_id, referred_id=activation.referred_id, slot=slot)


def share_payload(code: str, *, base_url: str = "https://carigma.in") -> dict[str, Any]:
    """What the share affordance renders.

    WhatsApp first: it is the dominant sharing channel for this audience, and
    a link that lands there as a bare URL looks like spam rather than a product.
    """
    link = f"{base_url}/r/{code}"
    message = (
        "I've been using Carigma to work on my LinkedIn and job search. "
        f"Here's {REFERRED_CREDITS} free credits if you want to try it: {link}"
    )
    return {
        "code": code,
        "link": link,
        "message": message,
        "whatsapp_url": f"https://wa.me/?text={_urlquote(message)}",
        "referred_credits": REFERRED_CREDITS,
        "referrer_credits": REFERRER_CREDITS,
        # Said up front, because someone about to share deserves to know the
        # reward is bounded before they ask five people.
        "cap": MAX_REFERRALS,
        "cap_note": (
            f"Up to {MAX_REFERRALS} friends — credits land once they upload a profile, "
            f"not when they sign up."
        ),
    }


def _urlquote(text: str) -> str:
    from urllib.parse import quote

    return quote(text, safe="")


__all__ = [
    "CODE_LENGTH",
    "MAX_REFERRALS",
    "REFERRED_CREDITS",
    "REFERRER_CREDITS",
    "Activation",
    "AlreadyReferred",
    "CapReached",
    "Grant",
    "LedgerKind",
    "SelfReferral",
    "State",
    "activation_from_profile_upload",
    "assert_referable",
    "grant_for_activation",
    "new_code",
    "next_slot",
    "share_payload",
]
