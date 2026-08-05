"""Credit rule tests.

    Credits are NEVER charged on failure or on an empty result.

This is the hardest rule in the product (Bible §13.6), so it gets tested at the
level where it is enforced — the service — rather than only through routes.
A fake store lets every branch be exercised, including the awkward ones.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from carigma_api.services import credits as credits_service
from carigma_api.services import runs as runs_service
from carigma_api.services.ai import UpstreamError
from carigma_api.services.credits import InsufficientCredits
from carigma_api.services.runs import RunReporter, RunStatus

USER = "user-1"


class FakeStore:
    def __init__(self, balance: int | None = 100) -> None:
        self.balance = balance
        self.ledger: list[tuple[int, str, int]] = []

    def read_balance(self, user_id: str) -> int | None:
        return self.balance

    def apply_delta(self, user_id: str, delta: int, reason: str, balance_after: int) -> None:
        self.balance = balance_after
        self.ledger.append((delta, reason, balance_after))


class ExplodingStore(FakeStore):
    def apply_delta(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("ledger unavailable")


async def _run(work: Any, store: FakeStore, action: str = "run_profile") -> Any:
    run = runs_service.new_run(USER, "profile")
    return await runs_service.execute(run, work, credit_store=store, action=action)


# ── The rule ───────────────────────────────────────────────────────────────


async def test_successful_run_charges_exactly_once() -> None:
    store = FakeStore(100)
    settled = await _run(lambda r: {"profileScore": 71}, store)

    assert settled.status is RunStatus.SUCCEEDED
    assert settled.credits.charged == 10  # run_profile
    assert store.balance == 90
    assert len(store.ledger) == 1


async def test_failed_run_charges_nothing() -> None:
    """The user got nothing. Billing them for our upstream's bad day is exactly
    what the rule forbids."""
    store = FakeStore(100)

    def boom(reporter: RunReporter) -> Any:
        raise UpstreamError("Claude is overloaded")

    settled = await _run(boom, store)

    assert settled.status is RunStatus.FAILED
    assert settled.credits.charged == 0
    assert store.balance == 100
    assert store.ledger == []


async def test_unexpected_exception_charges_nothing() -> None:
    """Not just UpstreamError — ANY failure must be free."""
    store = FakeStore(100)

    def boom(reporter: RunReporter) -> Any:
        raise ValueError("something nobody predicted")

    settled = await _run(boom, store)

    assert settled.status is RunStatus.FAILED
    assert settled.credits.charged == 0
    assert store.balance == 100


@pytest.mark.parametrize("empty", [None, [], {}, ""], ids=["none", "list", "dict", "str"])
async def test_empty_result_charges_nothing(empty: Any) -> None:
    """An empty result is an honest outcome, not a failure — and not billable.
    Nothing was fabricated to fill the gap, so nothing is charged for it."""
    store = FakeStore(100)
    settled = await _run(lambda r: empty, store)

    assert settled.status is RunStatus.EMPTY
    assert settled.credits.charged == 0
    assert store.balance == 100


async def test_cancelled_run_charges_nothing() -> None:
    store = FakeStore(100)

    def cancelled(reporter: RunReporter) -> Any:
        raise runs_service.RunCancelled

    settled = await _run(cancelled, store)

    assert settled.status is RunStatus.CANCELLED
    assert settled.credits.charged == 0
    assert store.balance == 100


async def test_zero_score_is_a_real_result_and_is_charged() -> None:
    """0 is a meaningful score, not an empty result. Treating falsy as empty
    would silently give away real work."""
    store = FakeStore(100)
    settled = await _run(lambda r: {"profileScore": 0}, store)

    assert settled.status is RunStatus.SUCCEEDED
    assert settled.credits.charged == 10


# ── Affordability ──────────────────────────────────────────────────────────


def test_check_affordable_rejects_when_short() -> None:
    store = FakeStore(3)
    with pytest.raises(InsufficientCredits) as exc:
        credits_service.check_affordable(store, USER, "run_profile")
    assert exc.value.required == 10
    assert exc.value.balance == 3


def test_check_affordable_allows_exact_balance() -> None:
    assert credits_service.check_affordable(FakeStore(10), USER, "run_profile") == 10


def test_unconfigured_credits_fail_open() -> None:
    """A missing credits table must not lock every user out of the product."""
    assert credits_service.check_affordable(FakeStore(None), USER, "run_profile") == 10


async def test_unconfigured_credits_charge_nothing_but_still_deliver() -> None:
    store = FakeStore(None)
    settled = await _run(lambda r: {"profileScore": 71}, store)
    assert settled.status is RunStatus.SUCCEEDED
    assert settled.credits.charged == 0


# ── Free actions ───────────────────────────────────────────────────────────


async def test_onboarding_score_is_free() -> None:
    """The first score is the product's promise; gating it would gate the one
    thing that proves the value."""
    store = FakeStore(100)
    settled = await _run(lambda r: {"profileScore": 62}, store, action="onboarding_score")

    assert settled.status is RunStatus.SUCCEEDED
    assert settled.credits.charged == 0
    assert store.balance == 100


def test_skip_with_reason_is_free() -> None:
    """The user is handing us training signal. Charging for it would be charging
    someone to help us improve the product (P2 brief §21 Q4)."""
    assert credits_service.cost_of("skip_slot") == 0


def test_onboarding_score_affordable_at_zero_balance() -> None:
    credits_service.check_affordable(FakeStore(0), USER, "onboarding_score")


# ── P2 pricing decisions ───────────────────────────────────────────────────


def test_naukri_run_matches_profile_run_price() -> None:
    """Identical pricing is easy to explain and avoids a two-tier perception
    where Naukri feels like the lesser track (§21 Q3)."""
    assert credits_service.cost_of("run_naukri") == credits_service.cost_of("run_profile") == 10


def test_regenerate_slot_is_much_cheaper_than_a_full_week() -> None:
    """12 generates an entire week; one slot is a fraction of that (§21 Q4)."""
    assert credits_service.cost_of("regenerate_slot") == 3
    assert credits_service.cost_of("regenerate_slot") < credits_service.cost_of("run_content")


# ── Awkward branches ───────────────────────────────────────────────────────


async def test_ledger_write_failure_does_not_fail_a_completed_run() -> None:
    """We under-charge rather than hand the user an error for work they already
    received."""
    store = ExplodingStore(100)
    settled = await _run(lambda r: {"profileScore": 71}, store)

    assert settled.status is RunStatus.SUCCEEDED
    assert settled.credits.charged == 0


def test_balance_moving_between_check_and_charge_delivers_unbilled() -> None:
    """A concurrent spend must not produce a negative balance, and must not
    make the user pay in wasted time for our race."""
    store = FakeStore(10)
    credits_service.check_affordable(store, USER, "run_profile")
    store.balance = 2  # another request spent in the meantime

    receipt = credits_service.charge_for_result(store, USER, "run_profile", {"ok": True})

    assert receipt.charged == 0
    assert store.balance == 2


async def test_concurrent_runs_never_overdraw() -> None:
    """Five simultaneous runs against a balance that affords two must not go
    negative."""
    store = FakeStore(25)  # affords 2 × run_profile (10 each)

    settled = await asyncio.gather(*(_run(lambda r: {"profileScore": 70}, store) for _ in range(5)))

    assert store.balance >= 0
    charged = sum(s.credits.charged for s in settled)
    assert charged <= 25


# ── Receipt shape ──────────────────────────────────────────────────────────


async def test_receipt_reports_human_readable_reason() -> None:
    store = FakeStore(100)
    settled = await _run(lambda r: {"profileScore": 71}, store)
    assert settled.credits.reason == "Profile Analyst run"


async def test_run_reports_v2_agent_label() -> None:
    """Agent names are locked to the V2 set (roadmap Part 0.2)."""
    store = FakeStore(100)
    settled = await _run(lambda r: {"profileScore": 71}, store)
    assert settled.as_dict()["agent_label"] == "Profile Analyst"
