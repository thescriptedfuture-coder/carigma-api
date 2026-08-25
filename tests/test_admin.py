"""Admin as an operational tool.

The rule under test throughout: **a blocked user who cannot act and does not
complain churns silently.** Everything about low-credit surfacing exists to
stop that, so these tests check that the urgent thing is genuinely surfaced
first rather than merely present somewhere.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from carigma_api.services.admin import (
    NEARLY_OUT_BELOW,
    Adjustment,
    AdjustmentRefused,
    CreditUrgency,
    LowCreditUser,
    Overview,
    activation_rate,
    apply_adjustment,
    low_credit_summary,
    rank_low_credit,
    retention_buckets,
    urgency_of,
)


def u(balance: int, *, email: str = "a@b.com", asked: bool = False) -> LowCreditUser:
    return LowCreditUser(
        user_id=f"u{balance}{'-a' if asked else ''}",
        email=email,
        balance=balance,
        has_open_request=asked,
    )


# ── Urgency ────────────────────────────────────────────────────────────────


def test_zero_is_blocked_not_merely_low() -> None:
    assert urgency_of(0) is CreditUrgency.BLOCKED
    assert urgency_of(-5) is CreditUrgency.BLOCKED


def test_below_the_cheapest_agent_run_is_nearly_out() -> None:
    """ "Nearly out" means "cannot run an agent" — the point at which the
    product stops being itself."""
    assert urgency_of(1) is CreditUrgency.NEARLY_OUT
    assert urgency_of(NEARLY_OUT_BELOW - 1) is CreditUrgency.NEARLY_OUT
    assert urgency_of(NEARLY_OUT_BELOW) is CreditUrgency.FINE


def test_blocked_sorts_before_nearly_out() -> None:
    """Someone at zero is blocked NOW; someone at 8 is merely close."""
    ranked = rank_low_credit([u(8), u(0), u(3)])
    assert [x.balance for x in ranked] == [0, 3, 8]


def test_someone_who_asked_outranks_someone_silent_at_the_same_tier() -> None:
    """They told us and we haven't answered — the silence is now ours."""
    ranked = rank_low_credit([u(0, email="silent@x.com"), u(0, email="asked@x.com", asked=True)])
    assert ranked[0].email == "asked@x.com"


def test_a_blocked_silent_user_still_outranks_a_nearly_out_asker() -> None:
    """Being blocked is the more urgent fact. Asking breaks ties within a
    tier; it does not jump tiers."""
    ranked = rank_low_credit([u(9, asked=True), u(0)])
    assert ranked[0].balance == 0


# ── The summary that greets the founder ────────────────────────────────────


def test_the_summary_splits_blocked_from_nearly_out() -> None:
    """ "3 blocked" is an instruction; "8 low" is a statistic."""
    summary = low_credit_summary([u(0), u(0), u(5), u(200)])

    assert summary["blocked"] == 2
    assert summary["nearly_out"] == 1
    assert summary["needs_action"] == 3


def test_healthy_users_are_not_in_the_action_list() -> None:
    summary = low_credit_summary([u(500), u(120)])

    assert summary["needs_action"] == 0
    assert summary["users"] == []


def test_the_headline_states_a_fact_rather_than_congratulating() -> None:
    assert low_credit_summary([u(500)])["headline"] == "Nobody is out of credits."


def test_the_headline_leads_with_the_blocked_count() -> None:
    headline = low_credit_summary([u(0), u(0), u(4), u(6, asked=True)])["headline"]

    assert headline.startswith("2 blocked at zero")
    assert "asked for more" in headline


def test_the_users_come_back_already_ordered() -> None:
    """The founder must not have to sort. A list you have to work at is a list
    that gets skipped."""
    summary = low_credit_summary([u(12), u(0), u(7)])
    assert [x["balance"] for x in summary["users"]] == [0, 7, 12]


def test_each_row_carries_an_actionable_one_liner() -> None:
    rows = low_credit_summary([u(0, asked=True), u(6)])["users"]

    assert rows[0]["line"] == "Blocked — 0 credits · asked for more"
    assert "can't run an agent" in rows[1]["line"]


# ── Manual adjustment ──────────────────────────────────────────────────────


def test_an_adjustment_requires_a_real_reason() -> None:
    """Unauditable six weeks later is the same as unrecorded."""
    with pytest.raises(AdjustmentRefused, match="Give a reason"):
        Adjustment(user_id="u1", delta=100, reason="", author="ravi")
    with pytest.raises(AdjustmentRefused, match="Give a reason"):
        Adjustment(user_id="u1", delta=100, reason="  x ", author="ravi")


def test_a_zero_adjustment_is_refused() -> None:
    with pytest.raises(AdjustmentRefused, match="nothing was written"):
        Adjustment(user_id="u1", delta=0, reason="testing", author="ravi")


def test_the_ledger_row_is_marked_as_an_admin_adjustment() -> None:
    """Distinct from purchase and signup on purpose: during a manual top-up
    beta, "credits issued" is meaningless unless given-away and sold can be
    told apart."""
    row = Adjustment(
        user_id="u1", delta=200, reason="ran out mid-tune-up", author="ravi"
    ).as_ledger_row(balance_after=200)

    assert row["kind"] == "admin_adjustment"
    assert row["delta"] == 200
    assert row["balance_after"] == 200


def test_the_ledger_row_records_who_did_it() -> None:
    row = Adjustment(user_id="u1", delta=50, reason="beta thank-you", author="ravi").as_ledger_row(
        balance_after=50
    )

    assert "beta thank-you" in row["reason"]
    assert "by ravi" in row["reason"]


def test_credits_can_be_removed_as_well_as_added() -> None:
    adj = Adjustment(user_id="u1", delta=-30, reason="duplicate grant", author="ravi")
    assert apply_adjustment(100, adj) == 70


def test_a_removal_clamps_at_zero_rather_than_creating_a_debt() -> None:
    """A negative balance has no meaning here — there is nothing to collect."""
    adj = Adjustment(user_id="u1", delta=-500, reason="reversing an error", author="ravi")
    assert apply_adjustment(100, adj) == 0


def test_a_debt_is_possible_only_when_explicitly_allowed() -> None:
    adj = Adjustment(user_id="u1", delta=-500, reason="reversing an error", author="ravi")
    assert apply_adjustment(100, adj, allow_negative=True) == -400


# ── Overview ───────────────────────────────────────────────────────────────


def test_the_error_rate_of_no_runs_is_zero_not_a_crash() -> None:
    assert Overview().error_rate == 0.0


def test_the_error_rate_is_a_real_fraction() -> None:
    assert Overview(runs_7d=100, runs_failed_7d=3).error_rate == 0.03


def test_issued_and_consumed_are_reported_separately() -> None:
    """Netting them into one figure hides both."""
    payload = Overview(credits_issued=5000, credits_consumed=1200).as_dict()

    assert payload["credits_issued"] == 5000
    assert payload["credits_consumed"] == 1200
    assert "credits_outstanding" not in payload


# ── Analytics ──────────────────────────────────────────────────────────────


def test_activation_with_no_signups_says_so_rather_than_dividing_by_zero() -> None:
    result = activation_rate(0, 0)
    assert result["rate"] is None
    assert result["note"] == "No signups yet."


def test_activation_is_reaching_a_first_score() -> None:
    assert activation_rate(50, 20)["rate"] == 0.4


def test_retention_only_counts_cohorts_that_have_had_the_chance() -> None:
    """Someone who signed up yesterday cannot yet be a day-7 datum, and
    including them would silently depress the number."""
    now = datetime(2026, 8, 8, tzinfo=UTC)
    yesterday = now - timedelta(days=1)

    buckets = retention_buckets([yesterday], {}, now=now)

    assert buckets["day_1"]["eligible"] == 1
    assert buckets["day_7"]["eligible"] == 0
    assert buckets["day_7"]["rate"] is None


def test_a_small_denominator_is_visible_rather_than_hidden() -> None:
    """A 100% day-30 rate from two users is not a 100% day-30 rate."""
    now = datetime(2026, 8, 8, tzinfo=UTC)
    old = now - timedelta(days=40)
    buckets = retention_buckets(
        [old], {old.isoformat(): [old + timedelta(days=30, hours=2)]}, now=now
    )

    assert buckets["day_30"]["rate"] == 1.0
    assert buckets["day_30"]["eligible"] == 1, "the denominator must travel with the rate"


def test_someone_active_only_on_day_one_is_not_day_seven_retained() -> None:
    now = datetime(2026, 8, 8, tzinfo=UTC)
    signup = now - timedelta(days=20)
    buckets = retention_buckets(
        [signup], {signup.isoformat(): [signup + timedelta(days=1, hours=1)]}, now=now
    )

    assert buckets["day_1"]["retained"] == 1
    assert buckets["day_7"]["retained"] == 0
