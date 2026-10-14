"""The signal row and the week strip — spec 3.2, and the zero that isn't one.

## What this is for

Today showed a five-card description of the product where the numbers belong:
"WHAT CARIGMA DOES · Career Scout · CV Architect · …". Marketing on the one
screen that exists to say what to do now, and half the surface spent on it.

The spec's answer is three numbers and seven dots: score with its movement,
pipeline, matches, and the week at a glance. All four already came from tables
`load_signals` was reading and discarding.

## The failure this file mostly guards

**A table that cannot be read must not render as a measured zero.** Every read
in `load_signals` was wrapped in `except: pass` into an empty list, which is
survivable for the ladder — a rung with nothing to say — and is a lie on a
signal row, where "0 active" is a statement about the user's job search. So
`Sources` keeps `None` (unreadable) apart from `[]` (nothing there), and every
builder below propagates the distinction instead of collapsing it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from carigma_api.services.posts import Slot, SlotState, WeekPlan
from carigma_api.services.today import (
    Sources,
    pipeline_signal,
    score_signal,
    signal_row,
    week_strip,
)

NOW = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)  # a Thursday
MONDAY = NOW.date() - timedelta(days=NOW.date().weekday())


def reading(days_ago: int, score: int) -> dict[str, object]:
    return {"score": score, "created_at": (NOW - timedelta(days=days_ago)).isoformat()}


def sources(**kw: object) -> Sources:
    base: dict[str, object] = {
        "now": NOW,
        "tracker": [],
        "jobs": [],
        "plan": None,
        "scores": [],
        "plan_read": True,
    }
    base.update(kw)
    return Sources(**base)  # type: ignore[arg-type]


# ── The score ──────────────────────────────────────────────────────────────


def test_an_unreadable_history_is_nothing_at_all_not_a_zero() -> None:
    assert score_signal(None) is None


def test_a_user_who_has_never_been_scored_has_no_value() -> None:
    """Two of twelve live profiles. `0` would be a verdict on their profile."""
    block = score_signal([])
    assert block == {"value": None, "delta_30d": None, "measured_at": None}


def test_the_latest_reading_wins_and_the_delta_is_against_the_window() -> None:
    block = score_signal([reading(0, 72), reading(20, 66), reading(90, 40)])
    assert block is not None
    assert block["value"] == 72
    # 66 is inside thirty days; 40 is not, and reaching past the window to
    # claim "+32" would be a number about a different quarter.
    assert block["delta_30d"] == 6


def test_a_single_reading_has_no_delta_rather_than_a_zero_one() -> None:
    """`0` reads as "no movement". There was no earlier measurement to move
    from, which is a different sentence."""
    block = score_signal([reading(2, 58)])
    assert block is not None
    assert block["value"] == 58
    assert block["delta_30d"] is None


def test_only_old_readings_means_no_delta() -> None:
    block = score_signal([reading(1, 70), reading(200, 50)])
    assert block is not None
    assert block["delta_30d"] is None


def test_a_decline_is_reported_as_a_decline() -> None:
    block = score_signal([reading(0, 61), reading(10, 64)])
    assert block is not None
    assert block["delta_30d"] == -3


# ── The pipeline ───────────────────────────────────────────────────────────


def test_an_unreadable_tracker_is_not_an_empty_pipeline() -> None:
    assert pipeline_signal(None) is None


def test_an_empty_tracker_is_a_real_zero() -> None:
    assert pipeline_signal([]) == {"active": 0, "interviewing": 0}


def test_only_live_stages_count_and_legacy_rejected_normalises() -> None:
    """`saved` is a bookmark, not an application. `offer` and `closed` are both
    finished. And one live row still says `rejected`, which predates the
    `closed` + reason split — so the normaliser is load-bearing, not
    defensive."""
    rows = [
        {"status": "saved"},
        {"status": "applied"},
        {"status": "interview"},
        {"status": "offer"},
        {"status": "rejected"},
    ]
    assert pipeline_signal(rows) == {"active": 2, "interviewing": 1}


# ── The week strip ─────────────────────────────────────────────────────────


def plan_with(*slots: Slot) -> WeekPlan:
    return WeekPlan(week_start=MONDAY, slots=tuple(slots))


def test_an_unreadable_plan_is_not_an_empty_week() -> None:
    """Seven grey dots is what both look like, so the difference dies here if
    it does not survive here."""
    assert week_strip(None, today=NOW.date(), plan_read=False) is None


def test_a_week_with_no_plan_is_seven_real_days() -> None:
    cells = week_strip(None, today=NOW.date())
    assert cells is not None
    assert [c["day"] for c in cells] == ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
    assert {c["state"] for c in cells} == {"none"}


def test_today_is_marked_once_and_on_the_right_day() -> None:
    cells = week_strip(None, today=NOW.date())
    assert cells is not None
    marked = [c for c in cells if c["is_today"]]
    assert len(marked) == 1
    assert marked[0]["day"] == "THU"


def test_each_slot_reports_its_own_state() -> None:
    cells = week_strip(
        plan_with(
            Slot(day="MON", slot_date=MONDAY, state=SlotState.PUBLISHED),
            Slot(day="THU", slot_date=MONDAY + timedelta(days=3), state=SlotState.DRAFT_READY),
            Slot(
                day="SAT",
                slot_date=MONDAY + timedelta(days=5),
                state=SlotState.PENDING,
                optional=True,
            ),
        ),
        today=NOW.date(),
    )
    assert cells is not None
    by_day = {c["day"]: c for c in cells}
    assert by_day["MON"]["state"] == "published"
    assert by_day["THU"]["state"] == "draft_ready"
    assert by_day["SAT"]["state"] == "pending"
    assert by_day["SAT"]["optional"] is True
    assert by_day["TUE"]["state"] == "none"


# ── The row as a whole ─────────────────────────────────────────────────────


def test_an_unreadable_feed_gives_no_matches_block() -> None:
    row = signal_row(sources(jobs=None), new_matches=None, open_matches=0)
    assert row["matches"] is None


def test_an_unknown_last_seen_keeps_new_null_while_open_stays_a_number() -> None:
    """`new: null` is "we do not know when you last looked" — four of twelve
    profiles. `open` is always knowable."""
    row = signal_row(sources(jobs=[{}]), new_matches=None, open_matches=12)
    assert row["matches"] == {"new": None, "open": 12, "top": []}


def test_every_block_can_be_absent_independently() -> None:
    row = signal_row(sources(tracker=None, scores=None, jobs=None), new_matches=1, open_matches=1)
    assert row == {"score": None, "pipeline": None, "matches": None}


# ── The named matches ──────────────────────────────────────────────────────


def test_an_unscored_job_sorts_last_rather_than_counting_as_zero() -> None:
    """A job nobody scored is not a bad match, and `None` is not `0`."""
    from carigma_api.services.today import matches_preview

    rows = [
        {"id": 1, "title": "A", "company": "X", "match_score": None, "status": "active"},
        {"id": 2, "title": "B", "company": "Y", "match_score": 71, "status": "active"},
        {"id": 3, "title": "C", "company": "Z", "match_score": 88, "status": "active"},
    ]
    assert [m["id"] for m in matches_preview(rows)] == [3, 2]


def test_dismissed_jobs_are_never_previewed() -> None:
    from carigma_api.services.today import matches_preview

    rows = [
        {"id": 1, "title": "A", "company": "X", "match_score": 99, "status": "dismissed"},
        {"id": 2, "title": "B", "company": "Y", "match_score": 40, "status": "active"},
    ]
    assert [m["id"] for m in matches_preview(rows)] == [2]


def test_no_jobs_is_an_empty_preview_not_a_crash() -> None:
    from carigma_api.services.today import matches_preview

    assert matches_preview([]) == []
    assert matches_preview(None) == []


# ── The table that had no rows ─────────────────────────────────────────────


def test_a_date_only_interview_does_not_crash_the_comparison() -> None:
    """`interview_on` holds `"2026-07-31"`.

    `datetime.fromisoformat` makes that NAIVE, and comparing a naive datetime
    to an aware `now` raises `TypeError` — which would have taken the whole of
    Today down rather than one rung, the first time anyone had an interview.
    """
    from carigma_api.services.today import next_interview

    rows = [
        {
            "id": "a1",
            "status": "interview",
            "company": "Zomato",
            "role": "Data Analyst",
            "interview_on": (NOW + timedelta(days=2)).date().isoformat(),
        }
    ]
    when, label, app_id = next_interview(rows, now=NOW)
    assert when is not None
    assert when.tzinfo is not None
    assert label == "Zomato, Data Analyst"
    # The card opens THAT application, not the board.
    assert app_id == "a1"


def test_a_finished_application_is_not_an_upcoming_interview() -> None:
    """One live row is `closed` with an `interview_on` in July."""
    from carigma_api.services.today import next_interview

    rows = [
        {
            "status": "closed",
            "company": "Paytm",
            "role": "Product Analyst",
            "interview_on": (NOW + timedelta(days=2)).date().isoformat(),
            "closed_reason": "Ghosted",
        }
    ]
    assert next_interview(rows, now=NOW) == (None, "", None)


def test_rejected_normalises_to_closed() -> None:
    from carigma_api.services.today import normalise_stage

    assert normalise_stage("rejected") == "closed"
    assert normalise_stage("Applied") == "applied"
    # Anything unrecognised is a bookmark, never silently "live".
    assert normalise_stage("") == "saved"
    assert normalise_stage(None) == "saved"
    assert normalise_stage("screening") == "saved"
