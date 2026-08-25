"""Scaling past one instance must be a decision, never an accident.

`services/ratelimit` holds token buckets in process memory. On two instances
every ceiling doubles — silently, against a JSearch quota live V1 users are
also drawing from.

The decision was to KEEP the in-process limiter (one instance is right at this
scale; a Redis for a problem we do not have is an operational burden acquired
before users) and make its constraint observable.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest

from carigma_api.services import instances
from tests.fake_supabase import FakeDB

NOW = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)


@pytest.fixture
def db() -> FakeDB:
    return FakeDB()


def seen(db: FakeDB, instance_id: str, *, ago: timedelta = timedelta()) -> None:
    db.seed(
        instances.TABLE,
        {"instance_id": instance_id, "last_seen": (NOW - ago).isoformat(), "version": "test"},
    )


# ── The observation ────────────────────────────────────────────────────────


def test_one_instance_is_not_a_problem(db: FakeDB) -> None:
    seen(db, "only-me")
    census = instances.census(db, now=NOW)

    assert census.count == 1
    assert census.is_overscaled is False


def test_two_live_instances_are_a_problem(db: FakeDB) -> None:
    seen(db, "a")
    seen(db, "b")

    assert instances.census(db, now=NOW).is_overscaled is True


def test_a_departed_instance_stops_counting(db: FakeDB) -> None:
    """Otherwise every past deploy would accumulate into a permanent alarm,
    and an alarm that is always on is an alarm nobody reads."""
    seen(db, "current")
    seen(db, "yesterday", ago=timedelta(hours=20))

    assert instances.census(db, now=NOW).count == 1


def test_the_window_tolerates_a_slow_heartbeat(db: FakeDB) -> None:
    """One late write must not read as a departure."""
    seen(db, "a")
    seen(db, "b", ago=instances.ALIVE_WINDOW - timedelta(seconds=30))

    assert instances.census(db, now=NOW).count == 2


# ── "We could not look" is not "there is one of us" ────────────────────────


def test_an_unreadable_table_reports_unknown_not_one(db: FakeDB) -> None:
    """The shape that made the retention bug and the empty weekly review
    survive: a failed read rendering as a confident answer."""
    db.failing.add(instances.TABLE)
    census = instances.census(db, now=NOW)

    assert census.count is None
    assert census.known is False
    # Unknown is not a breach — we do not cry wolf on a monitoring gap.
    assert census.is_overscaled is False


def test_a_failed_heartbeat_never_raises(db: FakeDB) -> None:
    """A monitoring gap must not be an outage. The limiter it protects is
    itself only a ceiling."""
    db.failing.add(instances.TABLE)
    instances.heartbeat(db, now=NOW)  # must not raise


def test_check_never_raises_when_everything_is_down(db: FakeDB) -> None:
    db.failing.add(instances.TABLE)
    assert instances.check(db, now=NOW).known is False


# ── The alarm ──────────────────────────────────────────────────────────────


def test_the_second_instance_produces_a_critical(
    db: FakeDB, caplog: pytest.LogCaptureFixture
) -> None:
    """The CRITICAL is the whole mechanism. Without it this is a table nobody
    reads, and scaling stays something that happened rather than something
    somebody chose."""
    seen(db, "a")
    seen(db, "b")

    with caplog.at_level(logging.CRITICAL):
        instances.check(db, now=NOW)

    assert any(r.levelno == logging.CRITICAL for r in caplog.records)
    message = " ".join(r.getMessage() for r in caplog.records)
    # It has to say WHAT breaks, or the reader cannot judge urgency.
    assert "rate limits" in message.lower()
    assert "jsearch" in message.lower()


def test_one_instance_produces_no_alarm(db: FakeDB, caplog: pytest.LogCaptureFixture) -> None:
    """Nothing seeded: `check` registers THIS process and finds only itself.

    (Seeding a peer here would make two, because the caller is one of them —
    which is the correct behaviour and was a wrong premise in the first draft
    of this test.)
    """
    with caplog.at_level(logging.WARNING):
        instances.check(db, now=NOW)

    assert [r for r in caplog.records if r.levelno >= logging.CRITICAL] == []


def test_heartbeat_registers_this_process(db: FakeDB) -> None:
    instances.check(db, now=NOW)
    ids = {r["instance_id"] for r in db.rows(instances.TABLE)}

    assert instances.INSTANCE_ID in ids


def test_a_second_heartbeat_updates_rather_than_duplicating(db: FakeDB) -> None:
    """Otherwise a single long-lived process would look like a fleet."""
    instances.check(db, now=NOW)
    instances.check(db, now=NOW + timedelta(seconds=40))

    assert len(db.rows(instances.TABLE)) == 1


# ── The payload admin sees ─────────────────────────────────────────────────


def test_the_report_explains_the_consequence(db: FakeDB) -> None:
    seen(db, "a")
    seen(db, "b")
    payload = instances.census(db, now=NOW).as_dict()

    assert payload["overscaled"] is True
    assert payload["note"] and "multiplied" in payload["note"]


def test_a_healthy_report_carries_no_scare_text(db: FakeDB) -> None:
    seen(db, "a")
    assert instances.census(db, now=NOW).as_dict()["note"] is None


def test_the_instance_id_is_per_process(db: FakeDB) -> None:
    """Two processes from one image are two instances, which is the thing
    being counted. An id derived from the release would report a fleet as one."""
    assert str(instances.INSTANCE_ID).count("-") >= 1
    assert instances.INSTANCE_ID != "carigma-api"
