"""The weekly review, which V2 could not produce.

`_facts_for(kind="weekly")` returned an empty `WeeklyFacts()` unconditionally.
`has_anything` was therefore always False, `build_weekly_review` always
returned None, and the review NEVER SENT.

It failed in the safe direction, which is why nobody noticed: skip-when-nothing
reported "nothing to report" rather than an error. A capability V1 users get
every Sunday was absent from V2 with nothing in the logs to say so.

This file exists so that silence cannot come back.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from carigma_api.services import emails as mail
from tests.fake_supabase import FakeDB

USER = "user-1"


def _load_script() -> Any:
    """Import `scripts/send_emails.py`, which is not a package module."""
    path = Path(__file__).resolve().parents[1] / "scripts" / "send_emails.py"
    spec = importlib.util.spec_from_file_location("send_emails_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


script = _load_script()


#: The cron's day — Sunday 18 Oct 2026, 18:00 IST — and the week it closes.
#: Pinned: `last_monday()` here used to compute the week the same way the code
#: did, from the real clock, so the test agreed with the bug on every day of
#: the week, including the Sunday the cron actually runs.
SUNDAY = date(2026, 10, 18)
REVIEWED = date(2026, 10, 12)


def last_monday() -> date:
    return REVIEWED


@pytest.fixture
def db() -> FakeDB:
    return FakeDB()


def seed_week(db: FakeDB, *, published: int, total: int) -> None:
    db.seed(
        "content_loop",
        *[
            {
                "user_id": USER,
                "id": i,
                "week_start": last_monday().isoformat(),
                "state": "published" if i < published else "draft_ready",
            }
            for i in range(total)
        ],
    )


# ── The gap itself ─────────────────────────────────────────────────────────


def test_a_real_week_now_produces_an_email(db: FakeDB) -> None:
    """The regression that matters. This returned None for every user, always."""
    seed_week(db, published=3, total=4)

    facts = script._facts_for(db, USER, "weekly", today=SUNDAY)
    email = mail.build_weekly_review("ravi@example.com", facts)

    assert facts.has_anything is True
    assert email is not None
    assert "3/4" in email.subject


def test_the_shipped_count_is_what_was_published(db: FakeDB) -> None:
    seed_week(db, published=2, total=5)
    facts = script._facts_for(db, USER, "weekly", today=SUNDAY)

    assert facts.shipped == 2
    assert facts.committed == 5


def test_only_the_closing_week_counts(db: FakeDB) -> None:
    """A review of the week that CLOSED. Counting next week's slots would
    report work that has not happened yet as done."""
    seed_week(db, published=1, total=1)
    db.seed(
        "content_loop",
        {
            "user_id": USER,
            "id": 99,
            "week_start": (last_monday() + timedelta(weeks=1)).isoformat(),
            "state": "published",
        },
    )
    facts = script._facts_for(db, USER, "weekly", today=SUNDAY)

    assert facts.committed == 1


def test_a_score_move_is_reported_with_its_direction(db: FakeDB) -> None:
    db.seed(
        "score_history",
        {"user_id": USER, "score": 64, "created_at": "2026-08-16T09:00:00Z"},
        {"user_id": USER, "score": 58, "created_at": "2026-08-09T09:00:00Z"},
    )
    facts = script._facts_for(db, USER, "weekly", today=SUNDAY)

    assert facts.score_lines == ("Your profile score moved up: 58 → 64.",)


def test_a_fall_is_stated_as_a_fall(db: FakeDB) -> None:
    """No euphemism. A number going down is information the user needs."""
    db.seed(
        "score_history",
        {"user_id": USER, "score": 55, "created_at": "2026-08-16T09:00:00Z"},
        {"user_id": USER, "score": 62, "created_at": "2026-08-09T09:00:00Z"},
    )
    facts = script._facts_for(db, USER, "weekly", today=SUNDAY)

    assert facts.score_lines == ("Your profile score moved down: 62 → 55.",)


def test_an_unchanged_score_is_not_a_line(db: FakeDB) -> None:
    db.seed(
        "score_history",
        {"user_id": USER, "score": 60, "created_at": "2026-08-16T09:00:00Z"},
        {"user_id": USER, "score": 60, "created_at": "2026-08-09T09:00:00Z"},
    )
    assert script._facts_for(db, USER, "weekly", today=SUNDAY).score_lines == ()


# ── The quiet week ─────────────────────────────────────────────────────────


def test_a_week_where_nothing_happened_sends_nothing(db: FakeDB) -> None:
    """Skip-when-nothing still holds. A review of an empty week is a
    manufactured email, and this feature's whole constraint is not doing that.
    """
    facts = script._facts_for(db, USER, "weekly", today=SUNDAY)
    assert facts.has_anything is False
    assert mail.build_weekly_review("ravi@example.com", facts) is None


def test_an_unsigned_plan_makes_a_quiet_week_worth_an_email(db: FakeDB) -> None:
    """The one exception, and it is a real one: the plan is waiting on THEM."""
    db.seed(
        "weekly_review",
        {
            "user_id": USER,
            "week_start": (last_monday() + timedelta(weeks=1)).isoformat(),
            "state": "proposed",
        },
    )
    facts = script._facts_for(db, USER, "weekly", today=SUNDAY)

    assert facts.contract_pending is True
    email = mail.build_weekly_review("ravi@example.com", facts)
    assert email is not None and "ready to approve" in email.body


def test_an_already_signed_plan_is_not_pending(db: FakeDB) -> None:
    """Asking someone to approve what they approved is a broken email."""
    db.seed(
        "weekly_review",
        {
            "user_id": USER,
            "week_start": (last_monday() + timedelta(weeks=1)).isoformat(),
            "state": "approved",
        },
    )
    assert script._facts_for(db, USER, "weekly", today=SUNDAY).contract_pending is False


# ── Degradation ────────────────────────────────────────────────────────────


def test_one_dead_source_costs_a_line_not_the_email(db: FakeDB) -> None:
    seed_week(db, published=2, total=3)
    db.failing.add("score_history")

    facts = script._facts_for(db, USER, "weekly", today=SUNDAY)

    assert facts.shipped == 2, "the healthy source still reports"
    assert facts.score_lines == ()


def test_a_total_outage_sends_nothing_rather_than_a_blank_review(db: FakeDB) -> None:
    """The direction that matters. An empty review claiming a quiet week when
    we simply could not read anything would be a lie about their week."""
    for table in ("content_loop", "score_history", "weekly_review"):
        db.failing.add(table)

    facts = script._facts_for(db, USER, "weekly", today=SUNDAY)
    assert mail.build_weekly_review("ravi@example.com", facts) is None


# ── Which week ─────────────────────────────────────────────────────────────


def test_on_its_sunday_the_review_reads_the_week_ending_that_day(db: FakeDB) -> None:
    """It read the week BEFORE: on 18 Oct, 5–11 Oct. It found nothing and
    skipped, with Ravi's first published post in the week it should have read."""
    seed_week(db, published=1, total=1)  # 12–18 Oct
    db.seed(
        "content_loop",
        {"user_id": USER, "id": 50, "week_start": "2026-10-05", "state": "draft_ready"},
    )

    facts = script._facts_for(db, USER, "weekly", today=SUNDAY)

    assert (facts.shipped, facts.committed) == (1, 1)
    assert facts.week_label == "12–18 Oct"


def test_a_monday_retry_reviews_the_same_week(db: FakeDB) -> None:
    seed_week(db, published=1, total=2)

    facts = script._facts_for(db, USER, "weekly", today=SUNDAY + timedelta(days=1))

    assert (facts.shipped, facts.committed) == (1, 2)


def test_the_pending_plan_is_next_weeks(db: FakeDB) -> None:
    """The plan Sunday's pass has just proposed, for the week starting
    tomorrow — not the week that is ending."""
    db.seed("weekly_review", {"user_id": USER, "week_start": "2026-10-12", "state": "proposed"})

    assert script._facts_for(db, USER, "weekly", today=SUNDAY).contract_pending is False

    db.seed("weekly_review", {"user_id": USER, "week_start": "2026-10-19", "state": "proposed"})

    assert script._facts_for(db, USER, "weekly", today=SUNDAY).contract_pending is True


# ── Sunday belongs to the review ───────────────────────────────────────────


def test_there_is_no_daily_brief_on_a_sunday(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both emails claim the day's one slot. The 08:30 brief won whenever
    anything was new, and the 18:00 review — the only email that asks for next
    week's signature — was refused as a duplicate."""

    class _Sunday(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:  # type: ignore[override]
            return datetime(2026, 10, 18, 3, 0, tzinfo=UTC)

    monkeypatch.setattr(script, "datetime", _Sunday)

    def no_settings() -> None:
        raise AssertionError("a Sunday brief got as far as reading settings")

    monkeypatch.setattr(script, "Settings", no_settings)

    summary = script.run("daily", dry_run=True)

    assert summary.failures == []


@pytest.mark.parametrize(
    ("today", "hours"),
    [(date(2026, 10, 19), 48), (date(2026, 10, 20), 24), (date(2026, 10, 17), 24)],
)
def test_mondays_brief_covers_the_sunday_that_had_none(today: date, hours: int) -> None:
    assert script.lookback_hours(today, None) == hours


def test_an_explicit_lookback_is_never_second_guessed() -> None:
    assert script.lookback_hours(date(2026, 10, 19), 6) == 6


def test_the_plan_link_goes_to_this_deploy_not_v1s_domain(db: FakeDB) -> None:
    """`carigma.in` serves V1 until cutover. "Ready to approve" linked there —
    a Streamlit app with no plan to approve."""
    facts = mail.WeeklyFacts(contract_pending=True)

    email = mail.build_weekly_review(
        "ravi@example.com", facts, app_url="https://carigma-web.onrender.com/"
    )

    assert email is not None
    assert "https://carigma-web.onrender.com/progress" in email.body
    assert "https://carigma.in" not in email.body
