"""Payments — verify-once, provably.

The central test here is `test_two_concurrent_verifications_credit_exactly_once`.
V1's reconciler performed a compare-and-swap and then credited unconditionally,
without checking whether the swap had actually won. Two concurrent reconciles
could therefore both credit one payment. These tests exist so V2 cannot.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from carigma_api.services.payments import (
    CREDIT_PACKS,
    LIVE_SUB_STATUSES,
    PLANS,
    GrantOutcome,
    PaymentStatus,
    pack,
    plan,
    pricing_payload,
    verify_signature,
    verify_subscription_cycles,
    verify_subscription_signature,
    verify_topup,
)

SECRET = "rzp_test_secret_value"


class FakeStore:
    """An in-memory stand-in whose `claim_*` methods are genuinely atomic.

    The lock matters: without it this fake would not be able to demonstrate the
    race it exists to rule out, and the concurrency test would pass for the
    wrong reason.
    """

    def __init__(
        self,
        payments: dict[str, dict[str, Any]] | None = None,
        subs: dict[str, dict[str, Any]] | None = None,
    ):
        self.payments = payments or {}
        self.subs = subs or {}
        self._lock = threading.Lock()
        self.claims_won = 0
        #: Called after a read returns, before the caller can claim. The
        #: concurrency tests use it to hold every thread at exactly the point
        #: where the interleaving matters. Without it the race window is a few
        #: microseconds wide and the test would pass or fail on timing luck —
        #: which is not a test.
        self.after_read: Any = None

    def record_payment(self, row: dict[str, Any]) -> None:
        self.payments[row["link_id"]] = row

    def get_payment(self, user_id: str, link_id: str) -> dict[str, Any] | None:
        row = self.payments.get(link_id)
        if not row or row.get("user_id") != user_id:
            return None
        # A COPY, because a real database read returns a snapshot. Returning
        # the live dict made the concurrency test pass for the wrong reason:
        # thread two saw thread one's write through the shared reference and
        # bailed at the early status check, never reaching the claim the test
        # exists to exercise.
        snapshot = dict(row)
        if self.after_read:
            self.after_read()
        return snapshot

    def claim_payment(self, user_id: str, link_id: str, payment_id: str | None) -> bool:
        with self._lock:
            row = self.payments.get(link_id)
            if not row or row.get("user_id") != user_id:
                return False
            if row["status"] != PaymentStatus.CREATED.value:
                return False
            row["status"] = PaymentStatus.PAID.value
            row["payment_id"] = payment_id
            self.claims_won += 1
            return True

    def mark_payment(self, user_id: str, link_id: str, status: str) -> None:
        row = self.payments.get(link_id)
        if row and row.get("user_id") == user_id:
            row["status"] = status

    def get_subscription(self, user_id: str, sub_id: str) -> dict[str, Any] | None:
        row = self.subs.get(sub_id)
        if not row or row.get("user_id") != user_id:
            return None
        snapshot = dict(row)  # snapshot, as above
        if self.after_read:
            self.after_read()
        return snapshot

    def claim_cycles(self, user_id: str, sub_id: str, *, seen: int, paid: int) -> int:
        with self._lock:
            row = self.subs.get(sub_id)
            if not row or row.get("user_id") != user_id:
                return 0
            # Guarded on the value the caller read — the CAS.
            if int(row.get("cycles_credited") or 0) != seen:
                return 0
            row["cycles_credited"] = paid
            return paid - seen


class FakeCredits:
    def __init__(self) -> None:
        self.balance = 0
        self.grants: list[tuple[int, str]] = []
        self._lock = threading.Lock()

    def grant(self, user_id: str, amount: int, reason: str) -> int:
        with self._lock:
            self.balance += amount
            self.grants.append((amount, reason))
            return self.balance


def a_payment(**over: Any) -> dict[str, Any]:
    return {
        "user_id": "u1",
        "link_id": "plink_1",
        "pack_key": "booster",
        "credits": 300,
        "amount_inr": 399,
        "status": PaymentStatus.CREATED.value,
        **over,
    }


# ── Verify-once: the rule that costs money ─────────────────────────────────


def test_a_paid_topup_credits_once() -> None:
    store = FakeStore({"plink_1": a_payment()})
    credits = FakeCredits()

    result = verify_topup(store, credits, user_id="u1", link_id="plink_1", provider_status="paid")

    assert result.outcome is GrantOutcome.GRANTED
    assert result.credits_added == 300
    assert credits.balance == 300


def test_verifying_the_same_payment_twice_credits_once() -> None:
    """The return-page refresh."""
    store = FakeStore({"plink_1": a_payment()})
    credits = FakeCredits()
    kw = {"user_id": "u1", "link_id": "plink_1", "provider_status": "paid"}

    first = verify_topup(store, credits, **kw)  # type: ignore[arg-type]
    second = verify_topup(store, credits, **kw)  # type: ignore[arg-type]

    assert first.outcome is GrantOutcome.GRANTED
    assert second.outcome is GrantOutcome.ALREADY_CREDITED
    assert second.credits_added == 0
    assert credits.balance == 300, "the payment was credited twice"
    assert len(credits.grants) == 1


def test_a_replayed_verification_still_reads_as_success_to_the_user() -> None:
    """Someone who refreshes must not be told their payment failed because the
    second request found nothing left to do."""
    store = FakeStore({"plink_1": a_payment()})
    credits = FakeCredits()
    kw = {"user_id": "u1", "link_id": "plink_1", "provider_status": "paid"}

    verify_topup(store, credits, **kw)  # type: ignore[arg-type]
    second = verify_topup(store, credits, **kw)  # type: ignore[arg-type]

    assert second.user_sees_success is True
    assert second.as_dict()["success"] is True
    assert "charged once" in second.message


def test_two_concurrent_verifications_credit_exactly_once() -> None:
    """THE test. V1 could fail this.

    Both threads read `created`, both attempt the swap, one wins. V1 credited
    from both because it never checked which. Here the claim gates the grant.
    """
    store = FakeStore({"plink_1": a_payment()})
    credits = FakeCredits()
    results: list[Any] = []
    # Both threads park here having each read `created`, then both proceed to
    # claim. This is the exact interleaving V1 mishandled.
    barrier = threading.Barrier(2)
    store.after_read = barrier.wait

    def run() -> None:
        results.append(
            verify_topup(store, credits, user_id="u1", link_id="plink_1", provider_status="paid")
        )

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Precondition: both attempts must have actually run, or "credited once"
    # would be true of a test that only ran one of them.
    assert len(results) == 2, "both verifications must have completed"

    assert store.claims_won == 1, "the database let two callers claim one payment"
    assert credits.balance == 300, f"double credit: {credits.grants}"
    assert sum(r.credits_added for r in results) == 300
    assert all(r.user_sees_success for r in results), "one caller was told it failed"


def test_a_payment_already_marked_paid_is_never_re_credited() -> None:
    store = FakeStore({"plink_1": a_payment(status=PaymentStatus.PAID.value)})
    credits = FakeCredits()

    result = verify_topup(store, credits, user_id="u1", link_id="plink_1", provider_status="paid")

    assert result.outcome is GrantOutcome.ALREADY_CREDITED
    assert credits.balance == 0


# ── Failure paths ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("status", ["expired", "cancelled", "failed"])
def test_a_dead_payment_credits_nothing_and_says_so_plainly(status: str) -> None:
    store = FakeStore({"plink_1": a_payment()})
    credits = FakeCredits()

    result = verify_topup(store, credits, user_id="u1", link_id="plink_1", provider_status=status)

    assert result.outcome is GrantOutcome.FAILED
    assert credits.balance == 0
    assert "haven't been charged" in result.message
    assert store.payments["plink_1"]["status"] == status


def test_an_abandoned_checkout_is_not_an_error() -> None:
    """The user opened the page and walked away. Nothing happened, and the copy
    says that rather than reporting a failure."""
    store = FakeStore({"plink_1": a_payment()})
    credits = FakeCredits()

    result = verify_topup(
        store, credits, user_id="u1", link_id="plink_1", provider_status="created"
    )

    assert result.outcome is GrantOutcome.NOT_PAID_YET
    assert credits.balance == 0
    assert store.payments["plink_1"]["status"] == PaymentStatus.CREATED.value


def test_one_users_payment_cannot_be_claimed_by_another() -> None:
    store = FakeStore({"plink_1": a_payment(user_id="u1")})
    credits = FakeCredits()

    result = verify_topup(store, credits, user_id="u2", link_id="plink_1", provider_status="paid")

    assert result.outcome is GrantOutcome.FAILED
    assert credits.balance == 0
    assert store.payments["plink_1"]["status"] == PaymentStatus.CREATED.value


def test_an_unknown_link_says_the_same_thing_as_someone_elses() -> None:
    """A different message would confirm the link id exists."""
    store = FakeStore({"plink_1": a_payment(user_id="u1")})
    credits = FakeCredits()

    mine = verify_topup(store, credits, user_id="u2", link_id="plink_1", provider_status="paid")
    unknown = verify_topup(store, credits, user_id="u2", link_id="nope", provider_status="paid")

    assert mine.message == unknown.message


# ── Subscriptions ──────────────────────────────────────────────────────────


def a_sub(**over: Any) -> dict[str, Any]:
    return {
        "user_id": "u1",
        "sub_id": "sub_1",
        "plan_key": "plus",
        "status": "active",
        "cycles_credited": 0,
        **over,
    }


def test_the_first_paid_month_is_credited() -> None:
    store = FakeStore(subs={"sub_1": a_sub()})
    credits = FakeCredits()

    result = verify_subscription_cycles(
        store,
        credits,
        user_id="u1",
        sub_id="sub_1",
        provider_paid_count=1,
        provider_status="active",
    )

    assert result.outcome is GrantOutcome.GRANTED
    assert credits.balance == 250
    assert "1 paid month" in result.message


def test_months_offline_are_all_credited_at_once() -> None:
    """`paid_count` is cumulative, so the difference is exactly what is owed —
    the user who comes back after a quarter is not shortchanged."""
    store = FakeStore(subs={"sub_1": a_sub(plan_key="pro", cycles_credited=1)})
    credits = FakeCredits()

    result = verify_subscription_cycles(
        store,
        credits,
        user_id="u1",
        sub_id="sub_1",
        provider_paid_count=4,
        provider_status="active",
    )

    assert result.credits_added == 600 * 3
    assert "3 paid months" in result.message


def test_reconciling_twice_credits_the_same_cycle_once() -> None:
    store = FakeStore(subs={"sub_1": a_sub()})
    credits = FakeCredits()
    kw = {
        "user_id": "u1",
        "sub_id": "sub_1",
        "provider_paid_count": 2,
        "provider_status": "active",
    }

    first = verify_subscription_cycles(store, credits, **kw)  # type: ignore[arg-type]
    second = verify_subscription_cycles(store, credits, **kw)  # type: ignore[arg-type]

    assert first.credits_added == 500
    assert second.outcome is GrantOutcome.ALREADY_CREDITED
    assert second.credits_added == 0
    assert credits.balance == 500


def test_two_concurrent_reconciles_credit_a_cycle_once() -> None:
    store = FakeStore(subs={"sub_1": a_sub()})
    credits = FakeCredits()
    results: list[Any] = []
    barrier = threading.Barrier(2)
    store.after_read = barrier.wait

    def run() -> None:
        results.append(
            verify_subscription_cycles(
                store,
                credits,
                user_id="u1",
                sub_id="sub_1",
                provider_paid_count=1,
                provider_status="active",
            )
        )

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 2
    assert credits.balance == 250, f"double credit: {credits.grants}"


def test_a_subscription_with_no_new_cycles_grants_nothing() -> None:
    store = FakeStore(subs={"sub_1": a_sub(cycles_credited=3)})
    credits = FakeCredits()

    result = verify_subscription_cycles(
        store,
        credits,
        user_id="u1",
        sub_id="sub_1",
        provider_paid_count=3,
        provider_status="active",
    )

    assert result.outcome is GrantOutcome.ALREADY_CREDITED
    assert credits.balance == 0


def test_an_unknown_plan_key_grants_nothing_rather_than_guessing() -> None:
    """Inventing a credit amount for a plan we cannot read would be making up
    what someone paid for."""
    store = FakeStore(subs={"sub_1": a_sub(plan_key="enterprise")})
    credits = FakeCredits()

    result = verify_subscription_cycles(
        store,
        credits,
        user_id="u1",
        sub_id="sub_1",
        provider_paid_count=1,
        provider_status="active",
    )

    assert result.outcome is GrantOutcome.FAILED
    assert credits.balance == 0
    assert "Nothing was changed" in result.message


def test_provider_count_behind_our_own_never_claws_back() -> None:
    """A stale read from Razorpay must not produce a negative grant."""
    store = FakeStore(subs={"sub_1": a_sub(cycles_credited=5)})
    credits = FakeCredits()

    result = verify_subscription_cycles(
        store,
        credits,
        user_id="u1",
        sub_id="sub_1",
        provider_paid_count=2,
        provider_status="active",
    )

    assert result.credits_added == 0
    assert credits.balance == 0


# ── Signatures ─────────────────────────────────────────────────────────────


def test_a_valid_order_signature_passes() -> None:
    import hashlib
    import hmac

    sig = hmac.new(SECRET.encode(), b"order_1|pay_1", hashlib.sha256).hexdigest()
    assert verify_signature(order_id="order_1", payment_id="pay_1", signature=sig, secret=SECRET)


def test_a_tampered_signature_fails() -> None:
    assert not verify_signature(
        order_id="order_1", payment_id="pay_1", signature="deadbeef", secret=SECRET
    )


def test_a_signature_for_a_different_order_fails() -> None:
    import hashlib
    import hmac

    sig = hmac.new(SECRET.encode(), b"order_OTHER|pay_1", hashlib.sha256).hexdigest()
    assert not verify_signature(
        order_id="order_1", payment_id="pay_1", signature=sig, secret=SECRET
    )


def test_an_unconfigured_secret_refuses_rather_than_passing() -> None:
    """A payment path that verifies nothing when misconfigured is worse than
    one that refuses."""
    assert not verify_signature(order_id="o", payment_id="p", signature="anything", secret="")
    assert not verify_signature(order_id="o", payment_id="p", signature="", secret=SECRET)


def test_subscriptions_sign_the_operands_the_other_way_round() -> None:
    """Razorpay signs `payment_id|subscription_id` for subscriptions and
    `order_id|payment_id` for orders. Swapping them fails silently as "invalid
    signature", so both directions are pinned."""
    import hashlib
    import hmac

    sub_sig = hmac.new(SECRET.encode(), b"pay_1|sub_1", hashlib.sha256).hexdigest()

    assert verify_subscription_signature(
        subscription_id="sub_1", payment_id="pay_1", signature=sub_sig, secret=SECRET
    )
    # The order-shaped verifier must NOT accept it.
    assert not verify_signature(
        order_id="sub_1", payment_id="pay_1", signature=sub_sig, secret=SECRET
    )


# ── Pricing and the dormant state ──────────────────────────────────────────


def test_the_pricing_surface_explains_itself_when_payments_are_off() -> None:
    """Not a dead button. §25 exists to prevent controls that look live and do
    nothing — and here it would be a control about money."""
    payload = pricing_payload(enabled=False)

    assert payload["enabled"] is False
    assert set(payload["unavailable"]) == {"happened", "why", "next"}
    # It offers the real alternative that exists during beta.
    assert "top you up" in payload["unavailable"]["next"]


def test_prices_are_still_shown_while_dormant() -> None:
    """Hiding them would make the product look like it has no plan for money."""
    payload = pricing_payload(enabled=False)
    assert len(payload["packs"]) == len(CREDIT_PACKS)
    assert len(payload["plans"]) == len(PLANS)


def test_no_unavailable_block_when_payments_are_live() -> None:
    assert "unavailable" not in pricing_payload(enabled=True)


def test_money_travels_as_an_integer_not_a_formatted_string() -> None:
    for entry in pricing_payload(enabled=True)["packs"]:
        assert isinstance(entry["amount_inr"], int)
        assert "₹" not in str(entry["amount_inr"])


def test_the_never_expire_promise_travels_with_the_plan() -> None:
    for entry in pricing_payload(enabled=True)["plans"]:
        assert entry["credits_expire"] is False


def test_the_v1_prices_are_carried_over_unchanged() -> None:
    assert plan("plus") is not None and plan("plus").amount_inr == 199
    assert plan("pro") is not None and plan("pro").amount_inr == 399
    assert pack("booster") is not None and pack("booster").credits == 300


def test_an_unknown_pack_or_plan_is_none_not_a_default() -> None:
    """Falling back to a default pack would charge someone for something they
    did not choose."""
    assert pack("nope") is None
    assert plan("nope") is None


def test_live_sub_statuses_include_the_ones_that_mean_starting() -> None:
    """`created` and `authenticated` are pre-first-charge but real — treating
    them as dead would show "no plan" to someone who just subscribed."""
    assert {"created", "authenticated", "active"} <= LIVE_SUB_STATUSES
