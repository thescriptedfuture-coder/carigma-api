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
from carigma_api.services.jobs.scoring import Annotation
from carigma_api.services.jobs.store import annotate, persist

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
            echoed = []
            for row in self._payload:
                self._db.next_id += 1
                stored = {"id": self._db.next_id, **row}
                self._db.inserted.append(dict(row))
                self._db.rows.append(stored)
                echoed.append(dict(stored))
            return type("Res", (), {"data": echoed})()
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
        self.next_id = 100

    def table(self, _name: str) -> FakeTable:
        return FakeTable(self)


def test_a_first_scan_inserts_everything_as_new() -> None:
    db = FakeDB()
    result = persist(db, user_id="u1", jobs=[job(), job("BI Analyst")], now=NOW)

    assert (len(result.new), result.seen_again) == (2, 0)
    # Named, with the id the database gave each row, so a surface can link to
    # exactly the postings that are new.
    assert [(r["id"], r["title"]) for r in result.new] == [
        (101, "Data Analyst"),
        (102, "BI Analyst"),
    ]
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

    result = persist(db, user_id="u1", jobs=[existing], now=NOW)

    assert (len(result.new), result.seen_again) == (0, 1)
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
    # `last_seen` and the source's facts. Never `status`, never `first_seen`.
    assert len(db.updated) == 1
    assert set(db.updated[0]) == {"user_id", "dedup_key", "last_seen", "details"}


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

    result = persist(db, user_id="u1", jobs=[shared], now=NOW)
    assert (len(result.new), result.seen_again) == (1, 0)


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
    result = persist(db, user_id="u1", jobs=[], now=NOW)
    assert (result.new, result.seen_again) == ([], 0)
    assert db.inserted == []


def test_the_row_carries_only_keys_the_presenter_reads() -> None:
    """`details` is written as a literal rather than from `asdict`, so a field
    added to the model cannot appear in the payload with nothing rendering it."""
    db = FakeDB()
    persist(db, user_id="u1", jobs=[job()], now=NOW)

    details = db.inserted[0]["details"]
    assert set(details) == {
        "apply_url",
        "apply_options",
        "publisher",
        "source",
        "posted_at",
        "expires_at",
    }
    assert details["apply_options"] == [
        {"publisher": "LinkedIn", "url": "https://li/1", "is_direct": False}
    ]


# ── `details` is one value, and an update replaces it ──────────────────────


def _scored(match: int = 82) -> Annotation:
    return Annotation(
        match_score=match,
        why_match="SQL and Power BI.",
        matched_skills=["SQL"],
        missing_skills=["dbt"],
        key_requirements=["3+ years"],
    )


def test_a_score_is_added_to_the_posting_not_written_over_it() -> None:
    """THE bug. Thirteen real postings lost their apply link, publisher,
    source and posting date to a scoring write that replaced the jsonb
    instead of adding to it. Every one was un-appliable when Ravi opened it."""
    db = FakeDB()
    found = [job()]
    result = persist(db, user_id="u1", jobs=found, now=NOW)

    annotate(db, user_id="u1", jobs=found, annotations={0: _scored()}, stored=result.details)

    details = db.rows[0]["details"]
    assert details["apply_url"] == "https://example.com/j", "the apply link was written over"
    assert details["source"] == "jsearch"
    assert details["apply_options"], "the apply options were written over"
    assert details["matched_skills"] == ["SQL"]
    assert db.rows[0]["match_score"] == 82


def test_a_rescan_keeps_what_v1_stored_and_what_was_scored() -> None:
    """A seen-again row can carry V1's keys and an earlier score. The scan
    owns only the source's facts; everything else stays."""
    seen = job(posted_at=NOW)
    db = FakeDB(
        [
            {
                "user_id": "u1",
                "dedup_key": seen.dedupe_key,
                "status": "saved",
                "details": {
                    "cvTweaks": ["Lead with the PFMS rollout"],
                    "matched_skills": ["Power BI"],
                    "apply_url": "https://old.example.com/j",
                },
            }
        ]
    )

    result = persist(db, user_id="u1", jobs=[seen], now=NOW)
    annotate(db, user_id="u1", jobs=[seen], annotations={0: _scored(70)}, stored=result.details)

    details = db.rows[0]["details"]
    assert details["cvTweaks"] == ["Lead with the PFMS rollout"], "V1's analysis was lost"
    assert details["apply_url"] == "https://example.com/j", "the source's current link is the truth"
    assert details["posted_at"] == NOW.isoformat()
    assert details["matched_skills"] == ["SQL"], "a fresh score replaces the old one"
    assert db.rows[0]["status"] == "saved"


def test_a_fact_the_source_left_out_today_does_not_erase_the_stored_one() -> None:
    seen = job()  # no posted_at this time
    db = FakeDB(
        [
            {
                "user_id": "u1",
                "dedup_key": seen.dedupe_key,
                "status": "active",
                "details": {"posted_at": "2026-10-01T00:00:00+00:00"},
            }
        ]
    )

    persist(db, user_id="u1", jobs=[seen], now=NOW)

    assert db.rows[0]["details"]["posted_at"] == "2026-10-01T00:00:00+00:00"


def test_annotate_refuses_a_row_whose_stored_details_it_was_not_given() -> None:
    """An update from nothing is the bug. Better unscored than unappliable."""
    db = FakeDB([{"user_id": "u1", "dedup_key": job().dedupe_key, "details": {"apply_url": "x"}}])

    scores = annotate(db, user_id="u1", jobs=[job()], annotations={0: _scored()}, stored={})

    assert scores == {}
    assert db.updated == []
    assert db.rows[0]["details"] == {"apply_url": "x"}


# ── The key is V1's ────────────────────────────────────────────────────────


def test_the_key_matches_what_v1_stored_for_the_same_posting() -> None:
    """Measured from the live row: Ravi dismissed NuvoRetail's "Senior Analyst
    E-Commerce" in July, and V1 stored it under `7e90d297588b9dd4`. V2's own
    readable key could never match it, so the first real scan brought the
    posting back as new."""
    nuvo = job(title="Senior Analyst E-Commerce", company="NuvoRetail", location="New Delhi, IN")

    assert nuvo.dedupe_key == "7e90d297588b9dd4"


def test_a_dismissal_v1_recorded_survives_a_v2_scan() -> None:
    nuvo = job(title="Senior Analyst E-Commerce", company="NuvoRetail")
    db = FakeDB(
        [{"user_id": "u1", "dedup_key": "7e90d297588b9dd4", "status": "dismissed", "details": {}}]
    )

    result = persist(db, user_id="u1", jobs=[nuvo], now=NOW)

    assert result.new == []
    assert db.rows[0]["status"] == "dismissed"


def test_one_posting_reported_in_two_locations_is_one_row() -> None:
    """JSearch called one ETS posting "New Delhi, IN" at 04:41 and "IN" at
    04:47, and a location-bearing key filed it twice."""
    a = job(title="Senior Analyst, Market Insights", company="ETS", location="New Delhi, IN")
    b = job(title="Senior Analyst, Market Insights", company="ETS", location="IN")

    assert a.dedupe_key == b.dedupe_key
