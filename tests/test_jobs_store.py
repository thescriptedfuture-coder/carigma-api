"""Writing a scan into `jobs_feed`, and what "New today" is allowed to mean.

The table carries two timestamps for one reason: a rescan must UPDATE
`last_seen` on a row it has seen before and INSERT only genuinely new roles. Do
it the obvious way — upsert everything with a fresh `first_seen` — and the
badge reports the whole feed as new every morning forever.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from carigma_api.services.jobs.models import ApplyOption, NormalizedJob
from carigma_api.services.jobs.store import persist

NOW = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)


def job(title: str = "Data Analyst", company: str = "Zomato", **kw: Any) -> NormalizedJob:
    base: dict[str, Any] = {
        "source": "jsearch",
        "source_id": f"{title}-{company}",
        "title": title,
        "company": company,
        "location": "Gurugram, India",
        "apply_url": "https://example.com/j",
        "apply_options": (ApplyOption(publisher="LinkedIn", url="https://li/1", is_direct=False),),
    }
    base.update(kw)
    return NormalizedJob(**base)


class FakeTable:
    def __init__(self, db: FakeDB) -> None:
        self._db = db
        self._op: str | None = None
        self._payload: Any = None
        self._filters: dict[str, Any] = {}
        self._in: tuple[str, list[Any]] | None = None

    def select(self, *_a: Any, **_k: Any) -> FakeTable:
        self._op = "select"
        return self

    def insert(self, payload: Any) -> FakeTable:
        self._op, self._payload = "insert", payload
        return self

    def update(self, payload: Any) -> FakeTable:
        self._op, self._payload = "update", payload
        return self

    def eq(self, column: str, value: Any) -> FakeTable:
        self._filters[column] = value
        return self

    def in_(self, column: str, values: list[Any]) -> FakeTable:
        self._in = (column, values)
        return self

    def execute(self) -> Any:
        if self._db.failing_read and self._op == "select":
            raise RuntimeError("jobs_feed is down")
        if self._op == "insert":
            self._db.inserted.extend(self._payload)
            self._db.rows.extend(dict(r) for r in self._payload)
            return type("Res", (), {"data": list(self._payload)})()
        if self._op == "update":
            for row in self._db.rows:
                if all(str(row.get(c)) == str(v) for c, v in self._filters.items()):
                    self._db.updated.append({**self._filters, **self._payload})
                    row.update(self._payload)
            return type("Res", (), {"data": []})()

        hits = [
            r
            for r in self._db.rows
            if all(str(r.get(c)) == str(v) for c, v in self._filters.items())
        ]
        if self._in:
            column, values = self._in
            hits = [r for r in hits if r.get(column) in values]
        return type("Res", (), {"data": [dict(r) for r in hits]})()


class FakeDB:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = list(rows or [])
        self.inserted: list[dict[str, Any]] = []
        self.updated: list[dict[str, Any]] = []
        self.failing_read = False

    def table(self, _name: str) -> FakeTable:
        return FakeTable(self)


def test_a_first_scan_inserts_everything_as_new() -> None:
    db = FakeDB()
    new, again = persist(db, user_id="u1", jobs=[job(), job("BI Analyst")], now=NOW)

    assert (new, again) == (2, 0)
    assert all(r["first_seen"] == NOW.isoformat() for r in db.inserted)
    assert all(r["status"] == "active" for r in db.inserted)


def test_a_rescan_moves_last_seen_and_leaves_first_seen_alone() -> None:
    """The whole reason the table has two timestamps.

    Upsert the lot with a fresh `first_seen` and the badge says "12 new" every
    morning forever — the same lie as a counter that cannot count.
    """
    existing = job()
    db = FakeDB(
        [
            {
                "user_id": "u1",
                "dedup_key": existing.dedupe_key,
                "first_seen": "2026-09-01T09:00:00+00:00",
                "last_seen": "2026-09-01T09:00:00+00:00",
                "status": "active",
            }
        ]
    )

    new, again = persist(db, user_id="u1", jobs=[existing], now=NOW)

    assert (new, again) == (0, 1)
    assert db.inserted == []
    assert db.rows[0]["first_seen"] == "2026-09-01T09:00:00+00:00"
    assert db.rows[0]["last_seen"] == NOW.isoformat()


def test_a_dismissed_job_found_again_stays_dismissed() -> None:
    """`status = 'dismissed'` is a decision the user made about that posting.
    The posting is still live, so every scan finds it again."""
    seen = job()
    db = FakeDB(
        [
            {
                "user_id": "u1",
                "dedup_key": seen.dedupe_key,
                "first_seen": "2026-09-01T09:00:00+00:00",
                "status": "dismissed",
                "dismiss_reason": "Salary too low",
            }
        ]
    )

    persist(db, user_id="u1", jobs=[seen], now=NOW)

    assert db.rows[0]["status"] == "dismissed"
    assert db.rows[0]["dismiss_reason"] == "Salary too low"
    # Only the one column moved.
    assert db.updated == [
        {"user_id": "u1", "dedup_key": seen.dedupe_key, "last_seen": NOW.isoformat()}
    ]


def test_a_paid_apply_kit_survives_a_rescan() -> None:
    seen = job()
    db = FakeDB(
        [
            {
                "user_id": "u1",
                "dedup_key": seen.dedupe_key,
                "apply_kit": {"cv": {"name": "Ravi Kumar"}},
                "status": "active",
            }
        ]
    )
    persist(db, user_id="u1", jobs=[seen], now=NOW)
    assert db.rows[0]["apply_kit"] == {"cv": {"name": "Ravi Kumar"}}


def test_another_users_identical_job_is_not_mistaken_for_this_ones() -> None:
    """The unique key is `(user_id, dedup_key)`, and two people apply to the
    same roles all the time."""
    shared = job()
    db = FakeDB([{"user_id": "someone-else", "dedup_key": shared.dedupe_key, "status": "active"}])

    new, again = persist(db, user_id="u1", jobs=[shared], now=NOW)
    assert (new, again) == (1, 0)


def test_an_unreadable_feed_refuses_rather_than_inserting_blind() -> None:
    """Inserting without knowing what is there duplicates every row the user
    already has — and the unique index would refuse half of them mid-batch."""
    db = FakeDB()
    db.failing_read = True

    with pytest.raises(RuntimeError):
        persist(db, user_id="u1", jobs=[job()], now=NOW)
    assert db.inserted == []


def test_an_empty_scan_writes_nothing_at_all() -> None:
    db = FakeDB()
    assert persist(db, user_id="u1", jobs=[], now=NOW) == (0, 0)
    assert db.inserted == []


def test_the_row_carries_only_keys_the_presenter_reads() -> None:
    """`details` is written as a literal rather than from `asdict`, so a field
    added to the model cannot appear in the payload with nothing rendering it."""
    db = FakeDB()
    persist(db, user_id="u1", jobs=[job()], now=NOW)

    details = db.inserted[0]["details"]
    assert set(details) == {"apply_url", "apply_options", "publisher", "source", "posted_at"}
    assert details["apply_options"] == [
        {"publisher": "LinkedIn", "url": "https://li/1", "is_direct": False}
    ]
