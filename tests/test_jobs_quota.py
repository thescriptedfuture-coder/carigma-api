"""The shared quota: the cache in front of it, and the cap that was never built.

`config.py` has said since the beginning that the JSearch quota is "handled by
the jobs cache + daily cap". `jsearch_daily_cap` was read by exactly one caller
— the admin overview, which *displays* it — and `jobs_cache_ttl_hours` by
nothing at all.

So the mechanism protecting live V1 users from V2's testing was two numbers and
a comment. These are the tests that make it a mechanism.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from carigma_api.services.jobs.quota import (
    cached,
    fetches_today,
    query_key,
    refusal,
    remember,
)

NOW = datetime(2026, 10, 15, 14, 0, tzinfo=UTC)
CAP = 25


class FakeTable:
    def __init__(self, db: FakeDB) -> None:
        self._db = db
        self._filters: dict[str, Any] = {}
        self._gte: tuple[str, str] | None = None
        self._op: str | None = None
        self._payload: dict[str, Any] | None = None
        self._conflict: str | None = None

    def select(self, *_a: Any, **_k: Any) -> FakeTable:
        self._op = "select"
        return self

    def upsert(self, payload: dict[str, Any], **kw: Any) -> FakeTable:
        self._op, self._payload = "upsert", payload
        self._conflict = kw.get("on_conflict")
        return self

    def eq(self, column: str, value: Any) -> FakeTable:
        self._filters[column] = value
        return self

    def gte(self, column: str, value: Any) -> FakeTable:
        self._gte = (column, value)
        return self

    def execute(self) -> Any:
        if self._db.failing:
            raise RuntimeError("jobs_cache is down")
        if self._op == "upsert":
            assert self._payload is not None
            self._db.conflict_targets.append(self._conflict)
            self._db.rows = [
                r for r in self._db.rows if r.get("query_key") != self._payload.get("query_key")
            ]
            self._db.rows.append(dict(self._payload))
            return type("Res", (), {"data": [dict(self._payload)]})()

        hits = [
            r
            for r in self._db.rows
            if all(str(r.get(c)) == str(v) for c, v in self._filters.items())
        ]
        if self._gte:
            column, floor = self._gte
            hits = [r for r in hits if str(r.get(column, "")) >= str(floor)]
        return type("Res", (), {"data": [dict(r) for r in hits]})()


class FakeDB:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = list(rows or [])
        self.failing = False
        self.conflict_targets: list[str | None] = []

    def table(self, _name: str) -> FakeTable:
        return FakeTable(self)


def fetch_row(hours_ago: float, source: str = "jsearch") -> dict[str, Any]:
    return {
        "query_key": f"k{hours_ago}",
        "source": source,
        "payload": [],
        "fetched_at": (NOW - timedelta(hours=hours_ago)).isoformat(),
    }


# ── Cannot count is not counted zero ───────────────────────────────────────


def test_an_unreadable_count_is_none_and_none_refuses() -> None:
    """`0` grants permission to spend a quota that belongs to live V1 users.

    Not knowing must not do the same, and the recoverable direction is to skip
    a scan: a user waits, instead of a paying user losing a search.
    """
    db = FakeDB()
    db.failing = True

    assert fetches_today(db, now=NOW) is None
    assert refusal(db, cap=CAP, now=NOW) is not None
    assert "didn't spend any of it" in str(refusal(db, cap=CAP, now=NOW))


def test_no_fetches_today_is_a_real_zero_and_allows_the_scan() -> None:
    db = FakeDB([fetch_row(30)])  # yesterday
    assert fetches_today(db, now=NOW) == 0
    assert refusal(db, cap=CAP, now=NOW) is None


def test_the_cap_refuses_at_the_limit_not_past_it() -> None:
    db = FakeDB([fetch_row(1) | {"query_key": f"k{i}"} for i in range(CAP)])

    assert fetches_today(db, now=NOW) == CAP
    message = refusal(db, cap=CAP, now=NOW)
    assert message is not None
    # The number and the reset are both in the sentence: a limit you cannot see
    # is indistinguishable from a product that is broken.
    assert "25 of 25" in message
    assert "midnight UTC" in message
    assert "Nothing was charged" in message


def test_one_below_the_cap_still_goes() -> None:
    db = FakeDB([fetch_row(1) | {"query_key": f"k{i}"} for i in range(CAP - 1)])
    assert refusal(db, cap=CAP, now=NOW) is None


def test_only_the_capped_source_counts_against_the_cap() -> None:
    """Adzuna has its own free tier. The cap exists to protect the key V1 is
    using, not to throttle everything."""
    db = FakeDB([fetch_row(1, "adzuna") | {"query_key": f"a{i}"} for i in range(40)])
    assert fetches_today(db, now=NOW) == 0
    assert refusal(db, cap=CAP, now=NOW) is None


def test_yesterdays_fetches_do_not_count_against_today() -> None:
    db = FakeDB([fetch_row(26) | {"query_key": f"y{i}"} for i in range(CAP)])
    assert fetches_today(db, now=NOW) == 0


# ── The cache in front of it ───────────────────────────────────────────────


def test_a_fresh_cache_hit_is_returned_and_costs_no_quota() -> None:
    db = FakeDB(
        [
            {
                "query_key": "k",
                "source": "jsearch",
                "payload": [{"title": "Data Analyst"}],
                "fetched_at": (NOW - timedelta(hours=2)).isoformat(),
            }
        ]
    )
    assert cached(db, key="k", ttl_hours=48, now=NOW) == [{"title": "Data Analyst"}]


def test_a_stale_row_is_a_miss_rather_than_an_old_answer() -> None:
    """Postings are perishable over days. Serving a three-day-old list as
    though it were today's is the stale-digest problem with a job in it."""
    db = FakeDB(
        [
            {
                "query_key": "k",
                "source": "jsearch",
                "payload": [{"title": "Data Analyst"}],
                "fetched_at": (NOW - timedelta(hours=72)).isoformat(),
            }
        ]
    )
    assert cached(db, key="k", ttl_hours=48, now=NOW) is None


def test_an_unreadable_cache_is_a_miss_not_a_crash() -> None:
    db = FakeDB()
    db.failing = True
    assert cached(db, key="k", ttl_hours=48, now=NOW) is None


def test_an_unparseable_timestamp_is_a_miss() -> None:
    db = FakeDB([{"query_key": "k", "payload": [], "fetched_at": "whenever"}])
    assert cached(db, key="k", ttl_hours=48, now=NOW) is None


def test_the_cache_write_names_its_conflict_target() -> None:
    """`jobs_cache` is unique on `query_key` and its PRIMARY key is `id`. An
    upsert with no target resolves on the primary key, which is how the
    `profiles` 23505 went unnoticed for a week."""
    db = FakeDB()
    remember(db, key="k", source="jsearch", payload=[{"title": "x"}])
    assert db.conflict_targets == ["query_key"]


def test_a_failed_cache_write_never_loses_the_jobs() -> None:
    """The fetch is already paid for and the jobs are about to be shown."""
    db = FakeDB()
    db.failing = True
    remember(db, key="k", source="jsearch", payload=[{"title": "x"}])  # must not raise


# ── The key ────────────────────────────────────────────────────────────────


def test_the_key_is_stable_and_case_insensitive() -> None:
    a = query_key(role="Data Analyst", city="Delhi", source="jsearch")
    b = query_key(role="  data analyst ", city="delhi", source="jsearch")
    assert a == b


def test_different_roles_cities_and_sources_do_not_collide() -> None:
    base = query_key(role="Data Analyst", city="Delhi", source="jsearch")
    assert base != query_key(role="BI Analyst", city="Delhi", source="jsearch")
    assert base != query_key(role="Data Analyst", city="Mumbai", source="jsearch")
    assert base != query_key(role="Data Analyst", city="Delhi", source="adzuna")


def test_a_role_full_of_punctuation_still_makes_one_key() -> None:
    """`Business Analyst — ERP & Public Finance / MoD` is a real stored title,
    and the column is unique."""
    key = query_key(
        role="Business Analyst — ERP & Public Finance / MoD", city="New Delhi", source="jsearch"
    )
    assert key.startswith("jsearch:")
    assert len(key) < 120
