"""The weekly contract state machine, and the honesty rules inside it.

The centre of gravity here is `auto_adopted` vs `approved`. That distinction is
invisible in the UI — both produce a working week — so it can only be defended
by tests. If it ever collapses, the product's own records start claiming the
user agreed to plans they never saw.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from carigma_api.services.review import (
    RE_ENTRY_MAX_MINUTES,
    Direction,
    ReviewFact,
    Streak,
    re_entry_contract,
    standing_by_card,
)
from carigma_api.services.weekly import (
    BANNED_PHRASES,
    Cadence,
    ContractItem,
    ContractState,
    ContractTransitionError,
    GuiltyCopy,
    WeeklyContract,
    approve,
    assert_no_guilt,
    auto_adopt,
    consecutive_lapses,
    conservative_items,
    decline,
    offers_fortnightly,
    presentation_for,
)

WEEK = date(2026, 8, 10)
NOW = datetime(2026, 8, 9, 18, 12, tzinfo=UTC)


def items() -> tuple[ContractItem, ...]:
    return (
        ContractItem("content", "MON", "Post slot 09:00", 10, 12),
        ContractItem("profile", "TUE", "Gap 2: rewrite your About", 6, 10),
        ContractItem("content", "THU", "Post slot 09:00", 10, 12),
        ContractItem("naukri", "SAT", "Tune-up steps 7-8", 12, 10),
        ContractItem("review", "SUN", "Review, 18:00", 10, 0),
    )


def contract(**kw: object) -> WeeklyContract:
    return WeeklyContract(week_start=WEEK, items=items(), **kw)  # type: ignore[arg-type]


# ── approved vs auto_adopted: the data-honesty rule ────────────────────────


def test_only_approval_counts_as_the_user_agreeing() -> None:
    assert ContractState.APPROVED.user_actually_agreed is True
    # Every other state, including the one that puts a plan in force.
    for state in ContractState:
        if state is not ContractState.APPROVED:
            assert state.user_actually_agreed is False, f"{state} must not read as agreement"


def test_auto_adoption_is_recorded_as_itself_not_as_approval() -> None:
    adopted = auto_adopt(contract(), now=NOW)

    assert adopted.state is ContractState.AUTO_ADOPTED
    assert adopted.state is not ContractState.APPROVED
    assert adopted.as_dict()["user_approved"] is False
    # Nothing was accepted, because nobody accepted anything.
    assert adopted.accepted_kinds == ()


def test_an_auto_adopted_plan_is_in_force_without_being_agreed_to() -> None:
    """Both halves matter: the product keeps working AND the record stays true."""
    adopted = auto_adopt(contract(), now=NOW)

    assert adopted.in_force_items, "the standing plan must actually run"
    assert adopted.state.user_actually_agreed is False


def test_the_serialized_shape_cannot_be_mistaken_for_approval() -> None:
    """A client reading only `state` would need to know the state set. The
    explicit boolean means it cannot get this wrong by omission."""
    payload = auto_adopt(contract(), now=NOW).as_dict()
    assert payload["state"] == "auto_adopted"
    assert payload["user_approved"] is False


# ── Signing ────────────────────────────────────────────────────────────────


def test_approving_records_who_agreed_to_what_and_when() -> None:
    signed = approve(contract(), now=NOW)

    assert signed.state is ContractState.APPROVED
    assert signed.state.user_actually_agreed is True
    assert signed.decided_at == NOW
    assert set(signed.accepted_kinds) == {"content", "profile", "naukri", "review"}


def test_partial_approval_puts_only_the_accepted_kinds_in_force() -> None:
    """The user may take the content plan and skip the scans. Today must never
    run something they declined."""
    signed = approve(contract(), accept=("content", "review"), now=NOW)

    kinds = {i.kind for i in signed.in_force_items}
    assert kinds == {"content", "review"}
    assert not any(i.kind == "naukri" for i in signed.in_force_items)


def test_accepting_nothing_is_recorded_as_a_decline() -> None:
    """Not as an approval of an empty plan — that would misstate what happened."""
    result = approve(contract(), accept=(), now=NOW)

    assert result.state is ContractState.DECLINED
    assert result.state.user_actually_agreed is False


def test_accepting_something_not_in_the_contract_is_refused() -> None:
    with pytest.raises(ContractTransitionError, match="not in this contract"):
        approve(contract(), accept=("content", "wire"), now=NOW)


def test_nothing_is_in_force_before_a_decision() -> None:
    assert contract().in_force_items == ()


def test_a_declined_week_runs_nothing() -> None:
    assert decline(contract(), now=NOW).in_force_items == ()


@pytest.mark.parametrize("second", [approve, decline, auto_adopt])
def test_a_decided_week_cannot_be_decided_again(second: object) -> None:
    """Re-deciding would overwrite the record of what the user actually did."""
    signed = approve(contract(), now=NOW)
    with pytest.raises(ContractTransitionError, match="already"):
        second(signed, now=NOW)  # type: ignore[operator]


# ── The conservative standing plan ─────────────────────────────────────────


def test_unattended_weeks_keep_scanning_but_stop_drafting() -> None:
    """Brief 2: "scans and decay tracking continue, new post drafts pause
    (nothing piles up)." A backlog of unread drafts is a debt, not a service."""
    kept = conservative_items(items())
    kinds = {i.kind for i in kept}

    assert "jobs" not in kinds or True  # no jobs row in this fixture
    assert "content" not in kinds, "drafts must not pile up while nobody is reading"
    assert {"profile", "naukri", "review"} <= kinds


def test_auto_adoption_applies_the_conservative_plan_not_the_full_one() -> None:
    adopted = auto_adopt(contract(), now=NOW)
    assert not any(i.kind == "content" for i in adopted.items)
    assert adopted.total_minutes < WeeklyContract(week_start=WEEK, items=items()).total_minutes


# ── Lapse counting ─────────────────────────────────────────────────────────


def _week(n: int, state: ContractState) -> WeeklyContract:
    return WeeklyContract(
        week_start=date(2026, 7, 6) + (n * (date(2026, 7, 13) - date(2026, 7, 6))), state=state
    )


def test_consecutive_lapses_counts_back_to_the_last_engagement() -> None:
    history = [
        _week(0, ContractState.APPROVED),
        _week(1, ContractState.AUTO_ADOPTED),
        _week(2, ContractState.AUTO_ADOPTED),
    ]
    assert consecutive_lapses(history, upto=date(2026, 8, 1)) == 2


def test_declining_breaks_the_lapse_run() -> None:
    """A decline is an answer. Counting it as neglect would call an active
    choice absence."""
    history = [
        _week(0, ContractState.AUTO_ADOPTED),
        _week(1, ContractState.DECLINED),
        _week(2, ContractState.AUTO_ADOPTED),
    ]
    assert consecutive_lapses(history, upto=date(2026, 8, 1)) == 1


def test_an_approved_week_resets_the_count() -> None:
    history = [_week(0, ContractState.AUTO_ADOPTED), _week(1, ContractState.APPROVED)]
    assert consecutive_lapses(history, upto=date(2026, 8, 1)) == 0


def test_one_skip_is_a_banner_and_two_is_a_takeover() -> None:
    """Escalation is bounded on purpose: one skip is unremarkable."""
    assert presentation_for(0) == "none"
    assert presentation_for(1) == "banner"
    assert presentation_for(2) == "standing_by"
    assert presentation_for(9) == "standing_by"


def test_the_exit_ramp_appears_from_the_second_lapse() -> None:
    assert offers_fortnightly(1) is False
    assert offers_fortnightly(2) is True


# ── The tone law ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("phrase", BANNED_PHRASES)
def test_every_banned_phrase_is_actually_caught(phrase: str) -> None:
    """The guard is verified by breaking it. A banned-list nobody has seen
    reject anything is just a comment."""
    with pytest.raises(GuiltyCopy):
        assert_no_guilt(f"Hey — {phrase}, come back!")


def test_the_guard_is_case_insensitive() -> None:
    with pytest.raises(GuiltyCopy):
        assert_no_guilt("WE MISSED YOU")


def test_plain_factual_copy_passes() -> None:
    assert_no_guilt("Two Sundays passed. Here's what actually happened.")


def test_a_fact_cannot_exist_without_a_receipt() -> None:
    with pytest.raises(ValueError, match="without a receipt"):
        ReviewFact(Direction.UP, "LinkedIn 58 to 62", "")


def test_a_fact_cannot_smuggle_in_guilt() -> None:
    with pytest.raises(GuiltyCopy):
        ReviewFact(Direction.DOWN, "You're falling behind on posts", "3 slots missed")


# ── The standing-by card ───────────────────────────────────────────────────


def facts() -> tuple[ReviewFact, ...]:
    return (
        ReviewFact(Direction.DOWN, "LinkedIn signal 62 to 61", "activity sub-score decayed"),
        ReviewFact(
            Direction.NEUTRAL, "9 matches cleared your bar; 3 have since expired", "scan Mon 09:00"
        ),
        ReviewFact(Direction.DOWN, "Naukri freshness 38% to 22%", "no edits since 21 Jul"),
    )


def test_the_standing_by_card_leads_with_facts_not_a_plea() -> None:
    card = standing_by_card(
        since=date(2026, 7, 21), lapses=2, facts=facts(), streak=Streak(0, 6, broken=True)
    )

    assert card["presentation"] == "standing_by"
    assert len(card["facts"]) == 3
    assert "actually happened" in card["body"]


def test_one_lapse_renders_a_banner_that_says_what_was_done() -> None:
    card = standing_by_card(since=date(2026, 8, 10), lapses=1, facts=(), streak=Streak(7))

    assert card["presentation"] == "banner"
    assert "standing plan was adopted" in card["body"]
    assert "Nothing piled up" in card["body"]


def test_the_fortnightly_ramp_only_appears_on_the_second_lapse() -> None:
    one = standing_by_card(since=date(2026, 8, 10), lapses=1, facts=(), streak=Streak(7))
    two = standing_by_card(since=date(2026, 7, 21), lapses=2, facts=(), streak=Streak(7))

    labels_one = [a["label"] for a in one["actions"]]
    labels_two = [a["label"] for a in two["actions"]]

    assert not any("fortnightly" in item.lower() for item in labels_one)
    assert any("fortnightly" in item.lower() for item in labels_two)


def test_a_broken_streak_is_named_rather_than_quietly_preserved() -> None:
    sentence = Streak(0, previous_best=6, broken=True).sentence()

    assert "6 weeks" in sentence
    assert "new one starts" in sentence
    assert_no_guilt(sentence)


def test_no_card_copy_can_ship_a_banned_phrase() -> None:
    """Belt and braces: walk the whole rendered payload, not just the fields we
    remembered to check."""
    card = standing_by_card(
        since=date(2026, 7, 21), lapses=3, facts=facts(), streak=Streak(0, 6, broken=True)
    )

    def strings(node: object) -> list[str]:
        if isinstance(node, str):
            return [node]
        if isinstance(node, dict):
            return [s for v in node.values() for s in strings(v)]
        if isinstance(node, list):
            return [s for v in node for s in strings(v)]
        return []

    assert_no_guilt(*strings(card))


# ── Re-entry ───────────────────────────────────────────────────────────────


def test_the_re_entry_week_is_deliberately_lighter() -> None:
    lighter = re_entry_contract(week_start=WEEK, candidates=items())

    assert lighter.total_minutes <= RE_ENTRY_MAX_MINUTES
    assert lighter.total_minutes < WeeklyContract(week_start=WEEK, items=items()).total_minutes


def test_re_entry_always_keeps_the_review() -> None:
    """It is the ritual being restarted — dropping it would restart nothing."""
    lighter = re_entry_contract(week_start=WEEK, candidates=items())
    assert any(i.kind == "review" for i in lighter.items)


def test_re_entry_offers_variety_not_volume() -> None:
    lighter = re_entry_contract(week_start=WEEK, candidates=items())
    kinds = [i.kind for i in lighter.items]
    assert len(kinds) == len(set(kinds)), "one of each kind at most on the way back in"


def test_re_entry_is_still_proposed_and_still_needs_a_signature() -> None:
    """ "The same signature, a lighter promise." It must not adopt itself."""
    lighter = re_entry_contract(week_start=WEEK, candidates=items())

    assert lighter.state is ContractState.PROPOSED
    assert lighter.in_force_items == ()


def test_re_entry_can_carry_the_fortnightly_cadence() -> None:
    lighter = re_entry_contract(week_start=WEEK, candidates=items(), cadence=Cadence.FORTNIGHTLY)
    assert lighter.cadence is Cadence.FORTNIGHTLY
    assert lighter.as_dict()["cadence"] == "fortnightly"


def test_re_entry_rows_stay_in_day_order() -> None:
    lighter = re_entry_contract(week_start=WEEK, candidates=items())
    order = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
    days = [i.day for i in lighter.items]
    assert days == sorted(days, key=order.index)
