"""The producer behind the triggers.

Without this, P6-5 was the referral shape: real logic with nothing feeding it,
which looks finished and fires for nobody.

Every test here is against rows, through the real reader — the shape a fake
`Facts` would let us skip.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from carigma_api.services import trigger_facts as producer
from carigma_api.services.triggers import choose
from tests.fake_supabase import FakeDB

USER = "user-1"
NOW = datetime(2026, 8, 17, 9, 0, tzinfo=UTC)


def ago(days: int, hours: int = 0) -> str:
    return (NOW - timedelta(days=days, hours=hours)).isoformat()


@pytest.fixture
def db() -> FakeDB:
    return FakeDB()


# ── Bands ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("score", "band"),
    [
        (0, "INVISIBLE"),
        (39, "INVISIBLE"),
        (40, "EMERGING"),
        (59, "EMERGING"),
        (60, "CLEAR"),
        (79, "CLEAR"),
        (80, "MAGNETIC"),
        (100, "MAGNETIC"),
    ],
)
def test_band_boundaries(score: int, band: str) -> None:
    assert producer.band_for(score) == band


def test_a_score_change_inside_one_band_is_not_news(db: FakeDB) -> None:
    """61 to 64 is real and is not worth an interruption. The trigger is a
    CROSSING, not a change."""
    db.seed(
        "score_history",
        {"user_id": USER, "score": 64, "created_at": ago(0)},
        {"user_id": USER, "score": 61, "created_at": ago(7)},
    )
    assert producer.band_move(db, USER) is None


def test_a_crossing_carries_the_receipt_that_justifies_it(db: FakeDB) -> None:
    db.seed(
        "score_history",
        {"user_id": USER, "score": 62, "created_at": ago(0)},
        {"user_id": USER, "score": 55, "created_at": ago(7)},
    )
    move = producer.band_move(db, USER)

    assert move == {
        "from": "EMERGING",
        "to": "CLEAR",
        "improved": True,
        "receipt": "your score went from 55 to 62",
    }


def test_a_first_ever_score_is_not_a_crossing(db: FakeDB) -> None:
    """Nothing to compare against. "You moved to EMERGING" on a first run
    would be announcing the starting position as progress."""
    db.seed("score_history", {"user_id": USER, "score": 55, "created_at": ago(0)})
    assert producer.band_move(db, USER) is None


def test_a_fall_is_reported_as_a_fall(db: FakeDB) -> None:
    db.seed(
        "score_history",
        {"user_id": USER, "score": 55, "created_at": ago(0)},
        {"user_id": USER, "score": 62, "created_at": ago(7)},
    )
    move = producer.band_move(db, USER)
    assert move is not None and move["improved"] is False


# ── Matches ────────────────────────────────────────────────────────────────


def a_job(**kw: Any) -> dict[str, Any]:
    return {
        "user_id": USER,
        "id": 1,
        "title": "Senior Data Analyst",
        "company": "Zomato",
        "match_score": 92,
        "why_match": "SQL and Power BI",
        "status": "new",
        "first_seen": ago(0, 2),
        **kw,
    }


def test_a_strong_new_match_is_reported(db: FakeDB) -> None:
    db.seed("jobs_feed", a_job())
    match = producer.top_match(db, USER, now=NOW)
    assert match is not None and match["company"] == "Zomato"


def test_a_match_below_the_bar_is_left_in_the_feed(db: FakeDB) -> None:
    db.seed("jobs_feed", a_job(match_score=70))
    assert producer.top_match(db, USER, now=NOW) is None


def test_an_old_match_is_not_news(db: FakeDB) -> None:
    """Announcing week-old things is how a feature earns its unsubscribe."""
    db.seed("jobs_feed", a_job(first_seen=ago(9)))
    assert producer.top_match(db, USER, now=NOW) is None


def test_a_rescanned_job_is_not_a_new_one(db: FakeDB) -> None:
    """`first_seen`, not `last_seen`. A posting re-confirmed by today's scan
    would otherwise be news every single day."""
    db.seed("jobs_feed", a_job(first_seen=ago(30), last_seen=ago(0)))
    assert producer.top_match(db, USER, now=NOW) is None


def test_a_dismissed_match_is_not_resurrected(db: FakeDB) -> None:
    db.seed("jobs_feed", a_job(status="not_for_me"))
    assert producer.top_match(db, USER, now=NOW) is None


# ── Stale applications ─────────────────────────────────────────────────────


def an_application(**kw: Any) -> dict[str, Any]:
    return {
        "user_id": USER,
        "id": 7,
        "company": "Swiggy",
        "title": "Analyst",
        "stage": "applied",
        "applied_at": ago(producer.FOLLOW_UP_DAYS + 4),
        "updated_at": ago(producer.FOLLOW_UP_DAYS + 4),
        **kw,
    }


def test_an_application_past_the_window_is_surfaced(db: FakeDB) -> None:
    db.seed("tracker", an_application())
    stale = producer.stale_application(db, USER, now=NOW)
    assert stale is not None
    assert stale["company"] == "Swiggy"
    assert stale["days"] == producer.FOLLOW_UP_DAYS + 4


def test_a_recent_application_is_not_stale(db: FakeDB) -> None:
    db.seed("tracker", an_application(applied_at=ago(3), updated_at=ago(3)))
    assert producer.stale_application(db, USER, now=NOW) is None


def test_an_application_they_just_touched_is_left_alone(db: FakeDB) -> None:
    """They are on it. A nudge would be us talking over them."""
    db.seed("tracker", an_application(updated_at=ago(1)))
    assert producer.stale_application(db, USER, now=NOW) is None


def test_a_closed_application_is_not_waiting_on_anything(db: FakeDB) -> None:
    db.seed("tracker", an_application(stage="closed"))
    assert producer.stale_application(db, USER, now=NOW) is None


def test_the_longest_wait_is_the_one_named(db: FakeDB) -> None:
    """If two have gone quiet, the one that has waited longest is the one
    worth naming."""
    db.seed(
        "tracker",
        an_application(id=1, company="Older", applied_at=ago(40), updated_at=ago(40)),
        an_application(id=2, company="Newer", applied_at=ago(12), updated_at=ago(12)),
    )
    stale = producer.stale_application(db, USER, now=NOW)
    assert stale is not None and stale["company"] == "Older"


# ── Approvals and milestones ───────────────────────────────────────────────


def test_only_proposed_updates_are_waiting(db: FakeDB) -> None:
    """An applied update is finished; a declined one is a decision. Emailing
    about either asks a question they already answered."""
    db.seed(
        "profile_updates",
        {"user_id": USER, "id": 1, "state": "proposed"},
        {"user_id": USER, "id": 2, "state": "applied"},
        {"user_id": USER, "id": 3, "state": "declined"},
    )
    update = producer.profile_update(db, USER)
    assert update is not None and update["changes"] == 1


def test_a_milestone_only_at_a_round_number(db: FakeDB) -> None:
    """A milestone the user would not recognise as an achievement is a
    manufactured reason to email."""
    for count in (9, 11, 24):
        fresh = FakeDB()
        fresh.seed(
            "content_loop",
            *[{"user_id": USER, "id": i, "state": "published"} for i in range(count)],
        )
        assert producer.milestone(fresh, USER) is None, count


def test_the_tenth_post_is_announced_once(db: FakeDB) -> None:
    db.seed(
        "content_loop",
        *[{"user_id": USER, "id": i, "state": "published"} for i in range(10)],
    )
    reached = producer.milestone(db, USER)
    assert reached is not None
    # The identity is the COUNT, so the eleventh post cannot re-fire the tenth.
    assert reached["id"] == "posts-10"


def test_unpublished_slots_do_not_count(db: FakeDB) -> None:
    db.seed(
        "content_loop",
        *[{"user_id": USER, "id": i, "state": "draft_ready"} for i in range(10)],
    )
    assert producer.milestone(db, USER) is None


# ── Assembly ───────────────────────────────────────────────────────────────


def test_a_source_that_fails_contributes_nothing_not_a_zero(db: FakeDB) -> None:
    """The whole degradation policy.

    An email that fails to mention something is recoverable. An email that
    announces something we could not actually see is not.
    """
    db.seed("jobs_feed", a_job())
    db.failing.add("score_history")
    db.failing.add("tracker")

    facts = producer.facts_for(db, USER, now=NOW)

    assert facts.top_match is not None, "a healthy source still reports"
    assert facts.band_move is None
    assert facts.stale_application is None


def test_a_total_outage_produces_no_email_rather_than_a_wrong_one(db: FakeDB) -> None:
    for table in ("jobs_feed", "score_history", "tracker", "profile_updates", "content_loop"):
        db.failing.add(table)

    assert choose(producer.facts_for(db, USER, now=NOW)) is None


def test_a_lapsed_user_short_circuits_before_any_query(db: FakeDB) -> None:
    """The re-engagement sequence owns their inbox, so five queries whose
    result `choose()` will discard is work for nothing."""
    for table in ("jobs_feed", "score_history", "tracker", "profile_updates", "content_loop"):
        db.failing.add(table)

    facts = producer.facts_for(db, USER, lapsed=True, now=NOW)

    assert facts.lapsed is True
    assert choose(facts) is None


def test_the_producer_and_the_triggers_agree_end_to_end(db: FakeDB) -> None:
    """Rows in, one email out — the round trip a hand-built `Facts` skips."""
    db.seed("jobs_feed", a_job())
    db.seed("tracker", an_application())

    facts = producer.facts_for(db, USER, now=NOW)
    chosen = choose(facts)

    assert chosen is not None
    # Time-bound beats browsable, over real rows and not a constructed fixture.
    assert chosen.key == "stale_application"
    email = chosen.build("ravi@example.com", facts)
    assert email is not None and "Swiggy" in email.subject


def test_nothing_in_the_producer_writes() -> None:
    """A producer that marked things as seen would make "what would we send"
    a destructive question, and the dry run would stop being repeatable."""
    import ast
    from pathlib import Path

    tree = ast.parse(Path(producer.__file__).read_text(encoding="utf-8"))
    writes = [
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"insert", "update", "upsert", "delete", "rpc"}
    ]
    assert writes == [], f"the facts producer must be read-only: {writes}"
