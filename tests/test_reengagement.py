"""The lapsed-user digest.

Three rules, and the first is the one that needed designing rather than coding:

1. **"Stop permanently" is written, not derived.** A finished sequence must
   survive a change to the cadence table. This file's most important test
   edits `CADENCE` and asserts nobody comes back from the dead.
2. **Decay, don't sustain.** Weekly x3, fortnightly x3, monthly x2, stop.
3. **Market-facing, never user-shaming.** An email is the easiest place to
   shame someone into unsubscribing, and an unsubscribe is not a retry.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from carigma_api.services import reengagement as re_
from carigma_api.services.market import MarketItem
from carigma_api.services.reengagement import (
    CADENCE,
    LAPSE_AFTER,
    TOTAL_SENDS,
    Phase,
    Sequence,
    State,
    assert_market_facing,
    build_digest,
    close_on_return,
    due_sequences,
    has_lapsed,
    is_due,
    phase_for,
    record_send,
    start,
)
from carigma_api.services.weekly import GuiltyCopy

NOW = datetime(2026, 8, 12, 9, 0, tzinfo=UTC)


def seq(**kw) -> Sequence:
    return Sequence(id=1, user_id="u1", **kw)


def items(n: int = 2) -> list[MarketItem]:
    return [
        MarketItem(f"Headline {i}", "Detail", f"Source {i}", f"https://example.com/{i}")
        for i in range(n)
    ]


# ── Who is lapsed ──────────────────────────────────────────────────────────


def test_seven_days_without_a_sign_in_is_lapsed() -> None:
    assert has_lapsed(NOW - LAPSE_AFTER, NOW) is True
    assert has_lapsed(NOW - LAPSE_AFTER + timedelta(hours=1), NOW) is False


def test_someone_who_has_never_signed_in_is_new_not_lapsed() -> None:
    """Treating a missing timestamp as an ancient one would put someone into a
    lapsed flow on the day they registered."""
    assert has_lapsed(None, NOW) is False


# ── The cadence ────────────────────────────────────────────────────────────


def test_the_cadence_is_weekly_x3_fortnightly_x3_monthly_x2() -> None:
    assert [(p.label, p.interval.days, p.sends) for p in CADENCE] == [
        ("weekly", 7, 3),
        ("fortnightly", 14, 3),
        ("monthly", 30, 2),
    ]
    assert TOTAL_SENDS == 8


@pytest.mark.parametrize(
    ("sends_made", "expected"),
    [
        (0, "weekly"),
        (2, "weekly"),
        (3, "fortnightly"),
        (5, "fortnightly"),
        (6, "monthly"),
        (7, "monthly"),
    ],
)
def test_the_next_send_lands_in_the_right_phase(sends_made: int, expected: str) -> None:
    phase = phase_for(sends_made)
    assert phase is not None and phase.label == expected


def test_after_the_last_send_there_is_no_next_phase() -> None:
    assert phase_for(TOTAL_SENDS) is None


def test_the_interval_widens_as_the_sequence_goes_on() -> None:
    """ "A smaller true cadence is a relationship; a weekly pretence is churn
    with extra steps.\""""
    intervals = [p.interval for p in CADENCE]
    assert intervals == sorted(intervals), "the cadence must decay, never tighten"


def test_each_send_schedules_the_next_at_its_phase_interval() -> None:
    after_first = record_send(seq(sends_made=0), NOW)
    assert after_first["next_due_at"] == (NOW + timedelta(days=7)).isoformat()

    after_third = record_send(seq(sends_made=2), NOW)
    assert after_third["next_due_at"] == (NOW + timedelta(days=14)).isoformat()

    after_sixth = record_send(seq(sends_made=5), NOW)
    assert after_sixth["next_due_at"] == (NOW + timedelta(days=30)).isoformat()


# ── Stopping permanently ───────────────────────────────────────────────────


def test_the_last_send_writes_the_terminal_state_immediately() -> None:
    """Not left to be inferred later by something comparing counts."""
    update = record_send(seq(sends_made=TOTAL_SENDS - 1), NOW)

    assert update["state"] == str(State.FINISHED)
    assert update["closed_at"] == NOW.isoformat()
    assert update["next_due_at"] is None


def test_a_finished_sequence_is_never_due_again() -> None:
    finished = seq(
        sends_made=TOTAL_SENDS, state=State.FINISHED, next_due_at=NOW - timedelta(days=1)
    )

    assert is_due(finished, NOW) is False


def test_changing_the_cadence_does_NOT_resurrect_a_finished_sequence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE test this data model exists for.

    A count-based implementation would compare sends_made against the table:
    add a fourth weekly phase and everyone who finished at eight is suddenly
    below the new total, resurrected months later by a config edit nobody
    connected to them.

    Because `finished` is written rather than derived, the row is inert.
    """
    finished = seq(
        sends_made=TOTAL_SENDS,
        state=State.FINISHED,
        closed_at=NOW - timedelta(days=90),
        next_due_at=None,
    )

    # Someone widens the programme later.
    monkeypatch.setattr(re_, "CADENCE", (*CADENCE, Phase("quarterly", timedelta(days=90), 4)))
    monkeypatch.setattr(re_, "TOTAL_SENDS", TOTAL_SENDS + 4)

    assert re_.phase_for(finished.sends_made) is not None, "the new table DOES offer a next phase"
    # ...and it changes nothing, because terminality is not a comparison.
    assert re_.is_due(finished, NOW) is False
    assert re_.due_sequences([finished], NOW) == []


def test_only_active_sequences_are_ever_due() -> None:
    overdue = NOW - timedelta(days=1)
    rows = [
        seq(state=State.ACTIVE, next_due_at=overdue),
        seq(state=State.RETURNED, next_due_at=overdue, closed_at=overdue),
        seq(state=State.FINISHED, next_due_at=overdue, closed_at=overdue),
    ]

    assert [s.state for s in due_sequences(rows, NOW)] == [State.ACTIVE]


def test_an_active_sequence_with_no_due_date_is_not_guessed_at() -> None:
    """Unscheduled-but-active is a bug. Inventing a date would hide it."""
    assert is_due(seq(state=State.ACTIVE, next_due_at=None), NOW) is False


# ── Return and re-lapse ────────────────────────────────────────────────────


def test_signing_in_closes_the_sequence_as_history_rather_than_deleting_it() -> None:
    update = close_on_return(NOW)

    assert update["state"] == str(State.RETURNED)
    assert update["closed_at"] == NOW.isoformat()


def test_a_re_lapse_starts_a_NEW_sequence_at_phase_one() -> None:
    """Not a reset counter — a new episode. That is what makes "restarts only
    if they lapse again" true by construction, and it leaves the previous
    episode intact as history."""
    fresh = start("u1", NOW)

    assert fresh["sends_made"] == 0
    assert fresh["state"] == str(State.ACTIVE)
    assert fresh["next_due_at"] == NOW.isoformat()
    phase = phase_for(fresh["sends_made"])
    assert phase is not None and phase.label == "weekly"


# ── The content ────────────────────────────────────────────────────────────


def test_no_curated_research_means_no_digest_at_all() -> None:
    """`None`, not an empty digest. A hollow send is the manufactured contact
    this whole feature refuses."""
    assert build_digest(None) is None
    assert build_digest([]) is None


def test_the_digest_is_about_the_market_not_the_absence() -> None:
    digest = build_digest(items(2))

    assert digest is not None
    body = digest.body.lower()
    assert "market" in body
    for banned in ("miss", "behind", "inactive", "come back"):
        assert banned not in body


def test_every_claim_names_its_source() -> None:
    digest = build_digest(items(2))

    assert digest is not None
    for item in items(2):
        assert item.source_name in digest.body


def test_the_relevant_line_is_framed_as_market_information() -> None:
    """ "Three of this week's trending skills aren't on your profile" is a fact
    about the market that happens to intersect them — not a report on what they
    failed to do."""
    digest = build_digest(items(1), missing_skills=("dbt", "Airflow", "Snowflake"))

    assert digest is not None
    assert "3 of this week's trending skills" in digest.body
    assert "dbt, Airflow, Snowflake" in digest.body


@pytest.mark.parametrize(
    "phrase",
    ["We miss you", "Don't lose your progress", "You're falling behind", "Still interested?"],
)
def test_the_banned_strings_are_refused_before_they_can_be_sent(phrase: str) -> None:
    """`GuiltyCopy` is the shared base: some of these are caught by the older
    lapsed-grammar guard and some by the re-engagement vocabulary. A caller
    should not have to know which, so there is one type to catch."""
    with pytest.raises(GuiltyCopy):
        assert_market_facing(phrase, "Some body text.")


def test_the_check_runs_on_the_assembled_text_not_the_parts() -> None:
    """No combination of individually innocent fragments may slip through."""
    with pytest.raises(GuiltyCopy, match="falling behind"):
        assert_market_facing("This week in analytics", "Hiring is up. You're falling behind.")
