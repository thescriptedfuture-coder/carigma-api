"""Sunday evening: the week's review, then next week's plan.

Nothing ran either. `save_review` had no caller, so every review was null; no
week was ever proposed, so every Monday began "No week is signed yet". The
cases below are the ones that decide whether the ritual is honest: whose weeks
it touches, what it refuses to write, and that every line names its evidence.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from carigma_api.services.sunday import audience, closing_week, sunday
from carigma_api.services.weekly import (
    Cadence,
    ContractItem,
    ContractState,
    WeeklyContract,
)
from carigma_api.services.weekly_store import SupabaseContractStore, from_row
from tests.fake_supabase import FakeDB

USER = "user-ravi"
OTHER = "user-v1-only"

#: Sunday 18 Oct 2026, 18:00 IST — when the cron runs.
NOW = datetime(2026, 10, 18, 12, 30, tzinfo=UTC)
WEEK = date(2026, 10, 12)
NEXT = date(2026, 10, 19)


def _contract(
    week: date,
    state: ContractState = ContractState.APPROVED,
    *,
    days: tuple[str, ...] = ("THU",),
    cadence: Cadence = Cadence.WEEKLY,
) -> WeeklyContract:
    items = tuple(ContractItem("content", d, "One post slot", 10, 3) for d in days) + (
        ContractItem("review", "SUN", "Review, 18:00", 10, 0),
    )
    decided = state is not ContractState.PROPOSED
    return WeeklyContract(
        week_start=week,
        items=items,
        state=state,
        cadence=cadence,
        accepted_kinds=tuple(i.kind for i in items) if state is ContractState.APPROVED else (),
        decided_at=NOW - timedelta(days=6) if decided else None,
    )


@pytest.fixture
def db() -> FakeDB:
    fake = FakeDB()
    fake.seed(
        "profiles",
        {"user_id": USER, "cadence": 5, "platforms": ["linkedin", "naukri"], "onboarded": True},
        # On V1 only: a profile, and no week ever planned in V2.
        {"user_id": OTHER, "cadence": 3, "platforms": ["linkedin"], "onboarded": True},
    )
    return fake


def _save(db: FakeDB, *contracts: WeeklyContract, user: str = USER) -> None:
    for c in contracts:
        SupabaseContractStore(db).save(user, c)


def _row(db: FakeDB, week: date, user: str = USER) -> dict[str, Any] | None:
    return next(
        (
            r
            for r in db.rows("weekly_review")
            if r["user_id"] == user and r["week_start"] == week.isoformat()
        ),
        None,
    )


def _ravis_week(db: FakeDB) -> None:
    """Measured, 2026-10-19: signed one post on Thursday; published Monday's,
    outside the plan, on Sunday evening; two applications moved to offer; one
    role saved."""
    _save(db, _contract(WEEK))
    db.seed(
        "content_loop",
        {
            "user_id": USER,
            "week_start": "2026-10-12",
            "day": "MON",
            "slot_date": "2026-10-12",
            "state": "published",
            "published_at": "2026-10-18T16:36:07.013309+00:00",
        },
    )
    db.seed(
        "application_events",
        {
            "user_id": USER,
            "application_id": "a1",
            "event_type": "stage_offer",
            "created_at": "2026-10-14T17:46:55+00:00",
        },
        {
            "user_id": USER,
            "application_id": "a1",
            "event_type": "stage_offer",
            "created_at": "2026-10-14T17:47:36+00:00",
        },
        {
            "user_id": USER,
            "application_id": "a2",
            "event_type": "stage_saved",
            "created_at": "2026-10-18T07:47:08+00:00",
        },
        # Last week's — not this review's.
        {
            "user_id": USER,
            "application_id": "a3",
            "event_type": "stage_applied",
            "created_at": "2026-10-09T10:00:00+00:00",
        },
    )
    db.seed(
        "job_applications",
        {"user_id": USER, "id": "a1", "company": "Acme"},
        {"user_id": USER, "id": "a2", "company": "Globex"},
        {"user_id": USER, "id": "a3", "company": "Initech"},
    )


def _facts(db: FakeDB, week: date = WEEK) -> list[dict[str, Any]]:
    row = _row(db, week)
    assert row is not None and row.get("facts") is not None, "no review was stored"
    return list(row["facts"]["facts"])


# ── Which week ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("today", "monday"),
    [
        (date(2026, 10, 18), date(2026, 10, 12)),  # its own Sunday: THIS week
        (date(2026, 10, 19), date(2026, 10, 12)),  # a Monday retry: the same week
        (date(2026, 10, 21), date(2026, 10, 12)),
    ],
)
def test_the_closing_week_is_the_one_the_last_sunday_ended(today: date, monday: date) -> None:
    assert closing_week(today) == monday


# ── The record ─────────────────────────────────────────────────────────────


def test_ravis_week_is_reviewed_with_a_receipt_on_every_line(db: FakeDB) -> None:
    _ravis_week(db)

    report = sunday(db, now=NOW)

    facts = _facts(db)
    assert report.reviewed == 1
    assert [(f["text"], f["receipt"]) for f in facts] == [
        ("0 of 1 planned post published", "NOT MARKED PUBLISHED THU"),
        # 16:36 UTC is 22:06 in India, and it is when he MARKED it.
        ("1 post published outside your plan", "MARK-PUBLISHED SUN 22:06"),
        # Two moves to offer, one application: one offer.
        ("Saved 1 role", "TRACKER · GLOBEX"),
        ("Offer stage: 1 application", "TRACKER · ACME"),
    ]
    assert all(f["receipt"].strip() for f in facts)


def test_the_review_leaves_the_signature_on_its_row_alone(db: FakeDB) -> None:
    _ravis_week(db)

    sunday(db, now=NOW)

    row = _row(db, WEEK)
    assert row is not None
    after = from_row(row)
    assert after.state is ContractState.APPROVED
    assert after.decided_at is not None


def test_a_week_that_all_shipped_says_so_with_the_times(db: FakeDB) -> None:
    _save(db, _contract(WEEK, days=("MON", "THU")))
    db.seed(
        "content_loop",
        *[
            {
                "user_id": USER,
                "week_start": "2026-10-12",
                "day": day,
                "slot_date": d,
                "state": "published",
                "published_at": at,
            }
            for day, d, at in (
                ("MON", "2026-10-12", "2026-10-12T03:34:00+00:00"),
                ("THU", "2026-10-15", "2026-10-15T03:32:00+00:00"),
            )
        ],
    )

    sunday(db, now=NOW)

    first = _facts(db)[0]
    assert first["direction"] == "done"
    assert first["text"] == "2 of 2 planned posts published"
    assert first["receipt"] == "MARK-PUBLISHED MON 09:04 · THU 09:02"


def test_a_skip_is_a_fact_with_its_reason_not_a_failure(db: FakeDB) -> None:
    _save(db, _contract(WEEK, days=("MON", "THU")))
    db.seed(
        "content_loop",
        {
            "user_id": USER,
            "week_start": "2026-10-12",
            "day": "THU",
            "slot_date": "2026-10-15",
            "state": "skipped",
            "skip_reason": "no_time",
        },
    )

    sunday(db, now=NOW)

    texts = [f["text"] for f in _facts(db)]
    assert "Skipped Thursday's post — travelling / no time this week" in texts


def test_a_falling_score_is_stated_as_a_fall(db: FakeDB) -> None:
    _save(db, _contract(WEEK))
    db.seed(
        "score_history",
        {"user_id": USER, "score": 62, "created_at": "2026-10-10T18:15:03+00:00"},
        {"user_id": USER, "score": 58, "created_at": "2026-10-15T09:00:00+00:00"},
    )

    sunday(db, now=NOW)

    score = next(f for f in _facts(db) if "score" in f["text"])
    assert (score["direction"], score["text"]) == ("down", "Profile score 62 → 58")
    assert score["receipt"] == "SCORE · MEASURED THU 15 OCT"


def test_new_matches_are_counted_from_when_they_were_first_seen(db: FakeDB) -> None:
    _save(db, _contract(WEEK))
    db.seed(
        "jobs_feed",
        {"user_id": USER, "id": 1, "first_seen": "2026-10-13T02:00:00+00:00"},
        {"user_id": USER, "id": 2, "first_seen": "2026-10-16T02:00:00+00:00"},
        {"user_id": USER, "id": 3, "first_seen": "2026-10-06T02:00:00+00:00"},  # last week
    )

    sunday(db, now=NOW)

    assert {"text": "Career Scout found 2 new matches"}.items() <= next(
        f for f in _facts(db) if "Career Scout" in f["text"]
    ).items()


def test_a_quiet_week_is_stored_as_a_review_with_no_facts(db: FakeDB) -> None:
    """A real answer about a real week — never redrawn as "your first review"."""
    _save(db, _contract(WEEK, days=()))

    sunday(db, now=NOW)

    assert _facts(db) == []


def test_a_review_attaches_only_to_a_week_that_exists(db: FakeDB) -> None:
    """An upsert onto a missing row would have created a `proposed` week from
    the column defaults — one the sweep adopts as a lapse that never happened."""
    _save(db, _contract(WEEK - timedelta(weeks=1)))  # planned once, not this week

    report = sunday(db, now=NOW)

    assert _row(db, WEEK) is None
    assert report.reviewed == 0
    assert report.skipped["review: no week to attach to"] == 1


def test_one_unreadable_source_stores_no_review_at_all(db: FakeDB) -> None:
    """A stored review is the record of the week. One without its pipeline
    because a table timed out would say less happened than did."""
    _ravis_week(db)
    db.failing.add("application_events")

    report = sunday(db, now=NOW)

    row = _row(db, WEEK)
    assert row is not None and row.get("facts") is None
    assert report.failed == 1
    # And the review failing does not cost next week its plan.
    assert _row(db, NEXT) is not None


# ── Next week ──────────────────────────────────────────────────────────────


def test_next_week_is_proposed_from_the_users_own_days(db: FakeDB) -> None:
    _save(db, _contract(WEEK))

    report = sunday(db, now=NOW)

    row = _row(db, NEXT)
    assert row is not None
    proposed = from_row(row)
    assert proposed.state is ContractState.PROPOSED
    assert proposed.decided_at is None
    assert proposed.proposed_at == NOW
    # Cadence 5, on LinkedIn and Naukri: every weekday, the refresh, the review.
    assert [(i.kind, i.day) for i in proposed.items] == [
        ("content", "MON"),
        ("content", "TUE"),
        ("content", "WED"),
        ("content", "THU"),
        ("content", "FRI"),
        ("naukri", "SAT"),
        ("review", "SUN"),
    ]
    assert report.proposed == 1


def test_a_week_already_planned_is_not_replaced(db: FakeDB) -> None:
    _save(db, _contract(WEEK), _contract(NEXT, days=("WED",)))

    sunday(db, now=NOW)

    row = _row(db, NEXT)
    assert row is not None
    assert [i.day for i in from_row(row).items if i.kind == "content"] == ["WED"]
    assert from_row(row).state is ContractState.APPROVED


def test_nobody_who_has_never_planned_a_week_is_touched(db: FakeDB) -> None:
    """V1 and V2 share the database. A plan written for someone using V1 is
    adopted unanswered by the sweep, and their first V2 visit opens on a
    standing-by card about weeks they were never shown."""
    _save(db, _contract(WEEK))

    sunday(db, now=NOW)

    assert [r for r in db.rows("weekly_review") if r["user_id"] == OTHER] == []


# ── Cutover: SUNDAY_AUDIENCE ───────────────────────────────────────────────


def test_at_cutover_everyone_onboarded_gets_sundays_plan(db: FakeDB) -> None:
    """After cutover every user is a V2 user, and a new one who has never
    pressed Plan this week should still open Monday on a plan."""
    _save(db, _contract(WEEK))

    report = sunday(db, now=NOW, who="everyone")

    other = _row(db, NEXT, user=OTHER)
    assert other is not None
    # Their own cadence (3), not the planner's.
    assert [i.day for i in from_row(other).items if i.kind == "content"] == ["MON", "WED", "FRI"]
    assert report.proposed == 2


def test_someone_who_never_finished_setup_is_not_planned_for(db: FakeDB) -> None:
    db.seed("profiles", {"user_id": "user-half-way", "cadence": 3, "onboarded": False})

    assert "user-half-way" not in audience(db, "everyone")


def test_a_planner_stays_in_whatever_their_profile_says(db: FakeDB) -> None:
    _save(db, _contract(WEEK), user="user-no-flag")

    assert "user-no-flag" in audience(db, "everyone")


@pytest.mark.parametrize("setting", ["planners", "", "all", "true", "everyone,", "every one"])
def test_anything_but_the_exact_word_stays_with_the_planners(db: FakeDB, setting: str) -> None:
    """Same rule as EMAIL_AUDIENCE: a typo must not write plans for people
    still using V1."""
    _save(db, _contract(WEEK))

    assert audience(db, setting) == [USER]


def test_the_exact_word_is_read_forgivingly_for_case_and_spaces(db: FakeDB) -> None:
    assert audience(db, "  Everyone ") == sorted([USER, OTHER])


def test_one_unanswered_week_still_gets_next_weeks_plan(db: FakeDB) -> None:
    _save(db, _contract(WEEK - timedelta(weeks=1)), _contract(WEEK, ContractState.PROPOSED))

    sunday(db, now=NOW)

    assert _row(db, NEXT) is not None


def test_no_plan_is_proposed_where_the_standing_by_card_will_take_over(db: FakeDB) -> None:
    """Two unanswered weeks make Monday's Progress the standing-by card, whose
    door is re-entry — and re-entry refuses a week that already has a plan. A
    proposal here would leave that door answering "already has a plan" while
    the plan stays hidden behind the card."""
    _save(
        db,
        _contract(WEEK - timedelta(weeks=1), ContractState.AUTO_ADOPTED),
        _contract(WEEK, ContractState.PROPOSED),
    )

    report = sunday(db, now=NOW)

    assert _row(db, NEXT) is None
    assert report.skipped["proposal: standing by"] == 1


def test_a_fortnightly_week_is_followed_by_its_week_off(db: FakeDB) -> None:
    _save(db, _contract(WEEK, cadence=Cadence.FORTNIGHTLY))

    report = sunday(db, now=NOW)

    assert _row(db, NEXT) is None
    assert report.skipped["proposal: fortnightly off-week"] == 1


def test_no_profile_means_no_guessed_plan(db: FakeDB) -> None:
    db.tables["profiles"] = []
    _save(db, _contract(WEEK))

    report = sunday(db, now=NOW)

    assert _row(db, NEXT) is None
    assert report.skipped["proposal: no profile"] == 1


def test_a_dry_run_writes_nothing_and_says_what_it_would(db: FakeDB) -> None:
    _ravis_week(db)
    before = [dict(r) for r in db.rows("weekly_review")]

    report = sunday(db, now=NOW, dry_run=True)

    assert db.rows("weekly_review") == before
    assert (report.reviewed, report.proposed) == (1, 1)


def test_only_restricts_the_pass_to_one_user(db: FakeDB) -> None:
    _save(db, _contract(WEEK))
    _save(db, _contract(WEEK), user="user-smoke")

    sunday(db, now=NOW, only=USER)

    assert _row(db, NEXT) is not None
    assert _row(db, NEXT, user="user-smoke") is None


def test_a_long_pipeline_receipt_names_three_and_counts_the_rest(db: FakeDB) -> None:
    """Measured on Ravi's week: four companies, one 70 characters long, made a
    receipt longer than the fact it was evidence for."""
    _save(db, _contract(WEEK))
    names = [
        "HDFC Bank",
        "NIC (National Informatics Centre) / Ministry of Finance",
        "Deloitte",
        "Razorpay",
    ]
    for i, name in enumerate(names):
        db.seed("job_applications", {"user_id": USER, "id": f"i{i}", "company": name})
        db.seed(
            "application_events",
            {
                "user_id": USER,
                "application_id": f"i{i}",
                "event_type": "stage_interview",
                "created_at": f"2026-10-1{i + 3}T09:00:00+00:00",
            },
        )

    sunday(db, now=NOW)

    fact = next(f for f in _facts(db) if "interview" in f["text"])
    assert fact["text"] == "Moved to interview: 4 applications"
    assert fact["receipt"] == "TRACKER · HDFC BANK, NIC (NATIONAL INFORMATICS C…, DELOITTE +1 MORE"
