"""Razorpay payments — built complete, shipped dormant.

**The whole path exists and is tested; the flag is off.** Razorpay KYC is
deferred until 100+ users, and the alternative to building now is integrating
payments in a hurry at the exact moment money starts moving. So: full path,
test mode, `PAYMENTS_ENABLED=false`. Turning it on is adding live keys and
flipping a boolean — no code change.

## Verify-once, and why V1's version could double-credit

V1 reconciled top-ups like this:

    row = fetch pending payment
    if link.status == "paid":
        update payments set status='paid' where id=? and status='created'
        add_credits(...)                     # <-- unconditional

The update is a compare-and-swap, but **its result was never checked**. Two
concurrent reconciles (two tabs, or a return-redirect racing a page visit)
could both read `created`, both attempt the swap — one wins, one affects zero
rows — and then *both* call `add_credits`. The user is credited twice for one
payment.

V2 inverts it: **the swap's result gates the grant.** `claim_payment` returns
whether *this caller* was the one that flipped the row, and only that caller
credits. Everyone else gets `already_credited`. The database decides, once.

Subscriptions use the same shape via `cycles_credited`: the number of cycles we
have paid out is compared against Razorpay's `paid_count`, and the write is
guarded on the value we read.

## Why reconciliation and not webhooks

Inherited from V1 and still correct: the app confirms on return or next visit
by asking Razorpay for the real status. A webhook is an optimisation, not a
requirement, and a payment path that depends on an inbound HTTP call is a
payment path that silently breaks when the endpoint moves.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

logger = logging.getLogger(__name__)


# ── Pricing (carried over from V1 unchanged) ───────────────────────────────


@dataclass(frozen=True)
class CreditPack:
    key: str
    label: str
    credits: int
    amount_inr: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "credits": self.credits,
            # Integer paise/rupees on the wire; the client formats. The API
            # never sends a pre-formatted money string.
            "amount_inr": self.amount_inr,
        }


@dataclass(frozen=True)
class Plan:
    key: str
    label: str
    amount_inr: int
    credits: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "amount_inr": self.amount_inr,
            "credits_per_month": self.credits,
            # A core promise, so it travels with the plan rather than living
            # only in marketing copy.
            "credits_expire": False,
        }


CREDIT_PACKS: tuple[CreditPack, ...] = (
    CreditPack("starter", "Starter", 100, 149),
    CreditPack("booster", "Booster", 300, 399),
    CreditPack("power", "Power", 750, 899),
)

PLANS: tuple[Plan, ...] = (
    Plan("plus", "Carigma Plus", 199, 250),
    Plan("pro", "Carigma Pro", 399, 600),
)


def pack(key: str) -> CreditPack | None:
    return next((p for p in CREDIT_PACKS if p.key == key), None)


def plan(key: str) -> Plan | None:
    return next((p for p in PLANS if p.key == key), None)


# ── State ──────────────────────────────────────────────────────────────────


class PaymentStatus(StrEnum):
    CREATED = "created"
    PAID = "paid"
    FAILED = "failed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self is not PaymentStatus.CREATED

    @property
    def is_dead(self) -> bool:
        """Terminal, and not a success. The user was not charged."""
        return self in (PaymentStatus.FAILED, PaymentStatus.EXPIRED, PaymentStatus.CANCELLED)


def status_from_provider(raw: str) -> PaymentStatus | None:
    """Map Razorpay's status onto ours, or None if we do not recognise it.

    `verify_topup` used to compare against the literal tuple
    `("expired", "cancelled", "failed")` and then write `provider_status`
    straight into the row — a second declaration of the enum, and an
    unvalidated write. Razorpay has statuses we do not model (`partially_paid`
    is one), and storing one of those would leave a payment in a state nothing
    can resolve: not `created`, not any value we compare against.

    Returning None means "we do not know what this is", which is a reason to
    leave the row alone, not a reason to guess.
    """
    try:
        return PaymentStatus(raw)
    except ValueError:
        return None


#: Subscription statuses that mean "the user has a live or starting plan".
LIVE_SUB_STATUSES: frozenset[str] = frozenset(
    {"created", "authenticated", "active", "pending", "halted"}
)


class GrantOutcome(StrEnum):
    """What a verification attempt actually did."""

    GRANTED = "granted"
    #: The payment was real, but someone else already credited it. Not an
    #: error — this is the expected answer to a retry, and the client shows
    #: the same success state.
    ALREADY_CREDITED = "already_credited"
    NOT_PAID_YET = "not_paid_yet"
    FAILED = "failed"
    #: The money moved and the credits did NOT arrive.
    #:
    #: Distinct from FAILED, which means the payment itself did not go through
    #: and nobody was charged. This is the opposite: charged, not credited.
    #: It is the only outcome where the user is owed something, so it must
    #: never be reported as a success.
    GRANT_FAILED = "grant_failed"


#: Said to someone whose money moved and whose credits did not arrive.
#:
#: It admits the charge, does not blame them, and commits to a resolution
#: without promising a timeframe we cannot keep. It must never read as though
#: nothing happened — something did, and it cost them.
_OWED = (
    "Your payment went through but we couldn't add the credits. "
    "We can see it and we'll put it right — nothing further is needed from you."
)


@dataclass(frozen=True)
class VerificationResult:
    outcome: GrantOutcome
    credits_added: int = 0
    balance_after: int | None = None
    message: str = ""

    @property
    def user_sees_success(self) -> bool:
        """Both GRANTED and ALREADY_CREDITED are successes to the user.

        Someone who refreshes the return page must not be told their payment
        failed because a second request found nothing left to do.
        """
        return self.outcome in (GrantOutcome.GRANTED, GrantOutcome.ALREADY_CREDITED)

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": str(self.outcome),
            "success": self.user_sees_success,
            "credits_added": self.credits_added,
            "balance_after": self.balance_after,
            "message": self.message,
        }


# ── Signature verification ─────────────────────────────────────────────────


def verify_signature(*, order_id: str, payment_id: str, signature: str, secret: str) -> bool:
    """Razorpay's HMAC-SHA256 over `order_id|payment_id`.

    `compare_digest` rather than `==` — a timing-variable comparison on a
    signature is the textbook way to leak it a byte at a time. An unconfigured
    secret returns False rather than passing: a payment path that verifies
    nothing when misconfigured is worse than one that refuses.
    """
    if not secret or not signature:
        return False
    expected = hmac.new(
        secret.encode("utf-8"),
        f"{order_id}|{payment_id}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def verify_subscription_signature(
    *, subscription_id: str, payment_id: str, signature: str, secret: str
) -> bool:
    """Subscriptions sign `payment_id|subscription_id` — the opposite order to
    orders. Getting this backwards fails silently as "invalid signature", so it
    has its own function and its own test."""
    if not secret or not signature:
        return False
    expected = hmac.new(
        secret.encode("utf-8"),
        f"{payment_id}|{subscription_id}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


# ── The store seam ─────────────────────────────────────────────────────────


class PaymentStore(Protocol):
    """Whatever holds `payments` / `user_subscriptions`."""

    def record_payment(self, row: dict[str, Any]) -> None: ...

    def get_payment(self, user_id: str, link_id: str) -> dict[str, Any] | None: ...

    def claim_payment(self, user_id: str, link_id: str, payment_id: str | None) -> bool:
        """Flip created → paid, and report whether THIS caller did it.

        The return value is the whole idempotency mechanism. It must be the
        result of a conditional update (`... where status = 'created'`), not a
        read followed by a write.
        """
        ...

    def mark_payment(self, user_id: str, link_id: str, status: str) -> None: ...

    def get_subscription(self, user_id: str, sub_id: str) -> dict[str, Any] | None: ...

    def claim_cycles(self, user_id: str, sub_id: str, *, seen: int, paid: int) -> int:
        """Advance `cycles_credited` from `seen` to `paid`, returning how many
        cycles THIS caller won the right to credit. Guarded on `seen`."""
        ...


class CreditGranter(Protocol):
    def grant(self, user_id: str, amount: int, reason: str) -> int | None: ...


# ── Verification ───────────────────────────────────────────────────────────


def verify_topup(
    store: PaymentStore,
    credits: CreditGranter,
    *,
    user_id: str,
    link_id: str,
    provider_status: str,
    payment_id: str | None = None,
) -> VerificationResult:
    """Confirm a top-up and credit it AT MOST ONCE.

    `provider_status` is what Razorpay says right now. The caller fetches it;
    this function decides what it means and what may be written.
    """
    row = store.get_payment(user_id, link_id)
    if row is None:
        # Not this user's payment, or not ours at all. Same answer either way —
        # a different message would confirm the link id exists.
        return VerificationResult(GrantOutcome.FAILED, message="We couldn't find that payment.")

    theirs = status_from_provider(provider_status)

    if theirs is not None and theirs.is_dead:
        # Write the real outcome. Until this ran through a route, a dead
        # payment could sit at `created` forever, and "why does my payment say
        # nothing happened" has no answer in the data.
        store.mark_payment(user_id, link_id, theirs.value)
        return VerificationResult(
            GrantOutcome.FAILED,
            message="That payment didn't go through. You haven't been charged.",
        )

    if theirs is not PaymentStatus.PAID:
        if theirs is None:
            # Not an error — Razorpay has states we do not model. We report
            # what we can honestly say and leave the row untouched, so the next
            # reconcile sees it unchanged rather than stuck on a guess.
            logger.info("unmodelled payment status %r on %s", provider_status, link_id)
        return VerificationResult(
            GrantOutcome.NOT_PAID_YET,
            message="We haven't seen that payment complete yet.",
        )

    # Razorpay says paid. Everything below hinges on the claim.
    if row.get("status") == PaymentStatus.PAID.value:
        return VerificationResult(
            GrantOutcome.ALREADY_CREDITED,
            credits_added=0,
            message="Already added — you were charged once.",
        )

    won = store.claim_payment(user_id, link_id, payment_id)
    if not won:
        # Another request flipped the row between our read and our write. It
        # is crediting; we must not.
        logger.info("payment %s already claimed by a concurrent request", link_id)
        return VerificationResult(
            GrantOutcome.ALREADY_CREDITED,
            credits_added=0,
            message="Already added — you were charged once.",
        )

    amount = int(row.get("credits") or 0)  # falsy-ok: no credits recorded on a pack IS zero credits
    label = row.get("pack_key") or "top-up"

    try:
        balance = credits.grant(user_id, amount, f"Top-up · {label} (Razorpay)")
    except Exception:
        # The write itself raised. We do NOT know whether it landed, so the
        # claim stays: releasing it could double-credit. This is the case that
        # needs a person.
        logger.exception("credit grant raised after claiming payment %s", link_id)
        return VerificationResult(GrantOutcome.GRANT_FAILED, message=_OWED)

    if balance is None:
        # `grant` returns None only when the balance READ failed, which means
        # `apply_delta` was never called and provably nothing was applied. So
        # the claim is released and the next reconcile retries it — the credits
        # arrive on the user's next page visit instead of via a support ticket.
        #
        # Before this, None was passed straight through as `balance_after` on a
        # GRANTED result: the user was told "N credits added", the row said
        # `paid`, and every retry answered "already added — you were charged
        # once". Charged, uncredited, and self-confirming.
        logger.error("could not credit claimed payment %s; releasing the claim", link_id)
        store.mark_payment(user_id, link_id, PaymentStatus.CREATED.value)
        return VerificationResult(GrantOutcome.GRANT_FAILED, message=_OWED)

    return VerificationResult(
        GrantOutcome.GRANTED,
        credits_added=amount,
        balance_after=balance,
        message=f"{amount} credits added.",
    )


def verify_subscription_cycles(
    store: PaymentStore,
    credits: CreditGranter,
    *,
    user_id: str,
    sub_id: str,
    provider_paid_count: int,
    provider_status: str,
) -> VerificationResult:
    """Credit any monthly cycles Razorpay has charged that we have not paid out.

    Works after months offline: `paid_count` is cumulative, so the difference
    is exactly what is owed. `claim_cycles` is guarded on the count we read, so
    two concurrent reconciles cannot both pay the same cycle.
    """
    row = store.get_subscription(user_id, sub_id)
    if row is None:
        return VerificationResult(
            GrantOutcome.FAILED, message="We couldn't find that subscription."
        )

    the_plan = plan(str(row.get("plan_key") or ""))
    if the_plan is None:
        logger.error("subscription %s references unknown plan %s", sub_id, row.get("plan_key"))
        return VerificationResult(
            GrantOutcome.FAILED, message="We couldn't read that plan. Nothing was changed."
        )

    seen = int(row.get("cycles_credited") or 0)  # falsy-ok: nothing credited yet IS zero cycles
    owed = max(0, provider_paid_count - seen)
    if owed == 0:
        return VerificationResult(
            GrantOutcome.ALREADY_CREDITED,
            message="Up to date — every paid month has been credited.",
        )

    won = store.claim_cycles(user_id, sub_id, seen=seen, paid=provider_paid_count)
    if won <= 0:
        return VerificationResult(
            GrantOutcome.ALREADY_CREDITED,
            message="Up to date — every paid month has been credited.",
        )

    amount = the_plan.credits * won
    months = "month" if won == 1 else "months"

    try:
        balance = credits.grant(user_id, amount, f"{the_plan.label} · {won} {months} (Razorpay)")
    except Exception:
        # Unknown whether the write landed, so the counter stays advanced.
        # Same reasoning as the top-up path: only a provably unapplied grant is
        # safe to retry.
        logger.exception("credit grant raised after claiming cycles on %s", sub_id)
        return VerificationResult(GrantOutcome.GRANT_FAILED, message=_OWED)

    if balance is None:
        # The balance read failed, so nothing was applied. Wind `cycles_credited`
        # back to what we read, guarded on it still being the value we set —
        # the same conditional update, run in reverse. A subscriber whose grant
        # blipped gets their month on the next reconcile instead of silently
        # losing it, and the guard means a concurrent reconcile that has moved
        # the counter on wins rather than being clobbered.
        logger.error("could not credit claimed cycles on %s; winding back", sub_id)
        store.claim_cycles(user_id, sub_id, seen=provider_paid_count, paid=seen)
        return VerificationResult(GrantOutcome.GRANT_FAILED, message=_OWED)

    return VerificationResult(
        GrantOutcome.GRANTED,
        credits_added=amount,
        balance_after=balance,
        message=f"{amount} credits added for {won} paid {months}.",
    )


# ── The dormant state, said honestly ───────────────────────────────────────


def pricing_payload(*, enabled: bool) -> dict[str, Any]:
    """The pricing surface, including what it can and cannot do right now.

    When payments are off the client renders the prices and an explanation —
    **not a dead button.** A control that looks live and does nothing is the
    V1 bug class §25 exists to prevent, and here it would be a control about
    money.
    """
    payload: dict[str, Any] = {
        "enabled": enabled,
        "packs": [p.as_dict() for p in CREDIT_PACKS],
        "plans": [p.as_dict() for p in PLANS],
    }
    if not enabled:
        payload["unavailable"] = {
            "happened": "Buying credits isn't switched on yet.",
            "why": "We're in beta and still completing payment verification.",
            "next": "Out of credits? Message us and we'll top you up — no charge during beta.",
        }
    return payload
