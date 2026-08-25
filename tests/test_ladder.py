"""The priority ladder — testable without HTTP, ordering pinned as data.

The sequence is the product decision, so it gets an exact assertion rather
than a set of pairwise ones: five priorities expressed as if/elif are five
things a later edit can silently reorder, and a test that only checks "A beats
B" lets C and D swap unnoticed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from carigma_api.services.ladder import (
    INTERVIEW_HORIZON,
    PRIORITIES,
    Signals,
    choose,
)

NOW = datetime(2026, 8, 17, 9, 0, tzinfo=UTC)
FALLBACK_KEY = "standing"


def signals(**kw: object) -> Signals:
    return Signals(now=NOW, **kw)  # type: ignore[arg-type]


# ── The ordering, as data ──────────────────────────────────────────────────


def test_the_sequence_is_exactly_this() -> None:
    """The whole table in one assertion.

    Pairwise tests let a middle pair swap while every pair still passes. This
    is the sequence, and changing it means changing this line — which is a
    visible diff on a product decision rather than an accident in control flow.
    """
    assert [r.key for r in sorted(PRIORITIES, key=lambda r: r.rank)] == [
        "interview_soon",
        "post_day",
        "score_fix",
        "new_matches",
        "standing",
    ]


def test_the_ranks_are_unique_and_contiguous() -> None:
    """Two rungs sharing a rank makes the winner depend on tuple order, which
    is exactly the silent reordering this table exists to prevent."""
    ranks = sorted(r.rank for r in PRIORITIES)

    assert ranks == list(range(1, len(PRIORITIES) + 1))


def test_only_the_time_bound_rungs_are_marked_time_bound() -> None:
    """An interview and a post day cost a WINDOW. A fix and a queue do not."""
    flags = {r.key: r.time_bound for r in PRIORITIES}

    assert flags == {
        "interview_soon": True,
        "post_day": True,
        "score_fix": False,
        "new_matches": False,
        "standing": False,
    }


# ── The two tie-breaks, each decided rather than defaulted ──────────────────


def test_an_interview_beats_a_post_day() -> None:
    """A missed post publishes tomorrow; an unprepared interview is gone."""
    decision = choose(
        signals(
            next_interview_at=NOW + timedelta(hours=30),
            interview_label="Zomato, Data Analyst",
            post_due_today="MON",
        )
    )

    assert decision.primary["key"] == "interview_soon"


def test_a_fix_beats_new_matches() -> None:
    """A fix is completable in one action; a queue is browsable. Today's
    premise is ONE primary action, so the completable rung wins."""
    decision = choose(signals(pending_fix="Your headline is missing years", new_match_count=6))

    assert decision.primary["key"] == "score_fix"


# ── What loses keeps its urgency, not just its presence ────────────────────


def test_a_demoted_post_day_is_still_time_bound() -> None:
    """THE refinement.

    "2 new matches" as a quiet stat is honest — it is not time-bound. "Your
    Wednesday post is ready" reduced to a plain stat card loses the fact that
    it is TODAY, and a post day that silently slides is the cadence eroding
    with nobody noticing.
    """
    decision = choose(
        signals(
            next_interview_at=NOW + timedelta(hours=10),
            interview_label="Swiggy",
            post_due_today="WED",
        )
    )

    demoted = next(d for d in decision.deferred if d["key"] == "post_day")
    assert demoted["time_bound"] is True
    assert "WED" in demoted["title"]


def test_a_demoted_queue_is_not_dressed_up_as_urgent() -> None:
    """The other direction. Marking everything time-bound would make the flag
    meaningless and the screen a wall of alarms."""
    decision = choose(signals(pending_fix="Add years to your headline", new_match_count=3))

    demoted = next(d for d in decision.deferred if d["key"] == "new_matches")
    assert demoted["time_bound"] is False


def test_nothing_that_applied_is_silently_dropped() -> None:
    """A suppressed rung is information the user had a moment ago."""
    decision = choose(
        signals(
            next_interview_at=NOW + timedelta(hours=5),
            interview_label="Razorpay",
            post_due_today="MON",
            pending_fix="Add tools to your headline",
            new_match_count=4,
        )
    )

    assert decision.primary["key"] == "interview_soon"
    assert [d["key"] for d in decision.deferred] == ["post_day", "score_fix", "new_matches"]


def test_the_fallback_never_appears_underneath_something_real() -> None:
    """ "Nothing needs you right now" below a thing that needs you is a
    contradiction on one screen."""
    decision = choose(signals(new_match_count=2))

    assert decision.primary["key"] == "new_matches"
    assert "standing" not in [d["key"] for d in decision.deferred]


# ── The interview window ───────────────────────────────────────────────────


def test_an_interview_just_inside_the_horizon_counts() -> None:
    decision = choose(
        signals(
            next_interview_at=NOW + INTERVIEW_HORIZON, interview_label="X", post_due_today="MON"
        )
    )

    assert decision.primary["key"] == "interview_soon"


def test_an_interview_beyond_the_horizon_does_not_take_over() -> None:
    """Four days out is not today's problem, and treating it as one crowds out
    the thing that is."""
    decision = choose(
        signals(
            next_interview_at=NOW + INTERVIEW_HORIZON + timedelta(hours=1),
            interview_label="X",
            post_due_today="MON",
        )
    )

    assert decision.primary["key"] == "post_day"
    assert "interview_soon" not in [d["key"] for d in decision.deferred]


def test_an_interview_in_the_past_is_not_upcoming() -> None:
    decision = choose(
        signals(next_interview_at=NOW - timedelta(hours=1), interview_label="X", new_match_count=1)
    )

    assert decision.primary["key"] == "new_matches"


# ── The empty case ─────────────────────────────────────────────────────────


def test_nothing_pressing_says_so_rather_than_inventing_work() -> None:
    """A product that always has a task for you is one that manufactures them."""
    decision = choose(signals())

    assert decision.primary["key"] == "standing"
    assert decision.deferred == []
    assert "Nothing needs you" in decision.primary["title"]


def test_zero_matches_is_not_a_rung() -> None:
    """`new_match_count=0` must not produce "0 new matches" as a primary
    action — the falsy-zero trap in its user-facing form."""
    decision = choose(signals(new_match_count=0))

    assert decision.primary["key"] == "standing"


def test_every_rung_produces_a_primary_action_with_somewhere_to_go() -> None:
    """An action with no destination is the dead end this codebase keeps
    deleting."""
    cases = [
        signals(next_interview_at=NOW + timedelta(hours=2), interview_label="X"),
        signals(post_due_today="MON"),
        signals(pending_fix="something"),
        signals(new_match_count=1),
        signals(),
    ]

    seen = set()
    for case in cases:
        primary = choose(case).primary
        seen.add(primary["key"])
        assert primary["title"]
        assert primary["primary"]["label"]
        assert primary["primary"]["route"].startswith("/")

    assert seen == {r.key for r in PRIORITIES}, "a rung was never exercised"


def test_the_fallback_cannot_be_deferred_by_construction() -> None:
    """Structural, not filtered.

    `choose` used to build `deferred` from every applicable rung and then drop
    `standing` by name — a rule enforced by a string comparison. `FALLBACK`
    now lives outside `CONDITIONAL`, so it is not in the list deferrals come
    from and cannot appear there however the loop is edited.
    """
    from carigma_api.services.ladder import CONDITIONAL, FALLBACK

    assert FALLBACK not in CONDITIONAL
    assert FALLBACK.key not in {r.key for r in CONDITIONAL}


def test_choose_cannot_return_nothing() -> None:
    """Also structural. The empty case was an `assert`, which `python -O`
    strips — so the one guard against a screen with no primary action would
    have vanished in exactly the build where it mattered."""
    decision = choose(signals())

    assert decision.primary["key"] == FALLBACK_KEY


def test_the_interview_builder_refuses_to_invent_an_interview() -> None:
    """If the table and the builders ever disagree, say so loudly rather than
    rendering a card about an interview that is not there."""
    import pytest

    from carigma_api.services.ladder import PRIORITIES as _P

    interview = next(r for r in _P if r.key == "interview_soon")
    with pytest.raises(ValueError, match="no interview"):
        interview.build(signals())


# ── "We do not know when you last looked" is its own state ─────────────────


def test_an_unknown_last_seen_never_claims_everything_is_new() -> None:
    """Four of twelve live profiles have no `last_seen_jobs_at`.

    With no point to measure "since" from, "15 new matches" is a claim about a
    comparison that was never made — and it would be the FIRST thing a new
    user ever read on Today.
    """
    decision = choose(signals(new_match_count=None, open_match_count=15))

    assert decision.primary["key"] == "new_matches"
    assert "new" not in decision.primary["title"]
    assert "15 matches waiting" == decision.primary["title"]


def test_an_unknown_last_seen_does_not_hide_a_full_feed_either() -> None:
    """The other direction. Treating unknown as zero would show "nothing needs
    you" to someone with fifteen jobs sitting there."""
    decision = choose(signals(new_match_count=None, open_match_count=15))

    assert decision.primary["key"] != "standing"


def test_a_known_zero_is_still_a_known_zero() -> None:
    """`0` and `None` are different answers and must not converge: having
    looked and found nothing new is not the same as never having looked."""
    decision = choose(signals(new_match_count=0, open_match_count=15))

    assert decision.primary["key"] == "standing"


def test_a_known_count_says_new():  # type: ignore[no-untyped-def]
    decision = choose(signals(new_match_count=3, open_match_count=15))

    assert decision.primary["title"] == "3 new matches"


def test_one_match_is_singular_in_both_phrasings() -> None:
    known = choose(signals(new_match_count=1, open_match_count=9))
    unknown = choose(signals(new_match_count=None, open_match_count=1))

    assert known.primary["title"] == "1 new match"
    assert unknown.primary["title"] == "1 match waiting"
