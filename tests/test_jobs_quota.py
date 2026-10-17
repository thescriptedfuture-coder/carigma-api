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


# ── The wiring, which is what was actually missing ─────────────────────────


class _Inner:
    """A provider that counts how often it is really called."""

    name = "jsearch"

    def __init__(self, jobs: list[Any] | None = None, fail: bool = False) -> None:
        self.calls = 0
        self.jobs = jobs or []
        self.fail = fail

    def available(self) -> bool:
        return True

    def search(self, *, role: str, city: str, limit: int = 20) -> list[Any]:
        self.calls += 1
        if self.fail:
            raise RuntimeError("provider down")
        return list(self.jobs)


def _job() -> Any:
    from carigma_api.services.jobs.models import NormalizedJob

    return NormalizedJob(
        source="jsearch",
        source_id="1",
        title="Data Analyst",
        company="Zomato",
        location="Gurugram, India",
        apply_url="https://example.com/1",
    )


def test_a_real_call_leaves_a_row_the_cap_counts() -> None:
    """THE test that was missing.

    S5 built the counter and the refusal. S6 wired the refusal into the scan
    and never recorded a single call, so the count was permanently zero and the
    first two real scans spent shared JSearch quota without either being
    counted. Every piece was tested; the wiring between them was not.
    """
    from carigma_api.services.jobs.quota import RecordedProvider

    db = FakeDB()
    inner = _Inner([_job()])
    provider = RecordedProvider(inner, db, ttl_hours=48)

    assert fetches_today(db) == 0
    provider.search(role="Data Analyst", city="Delhi")

    assert inner.calls == 1
    assert fetches_today(db) == 1


def test_the_same_search_inside_the_ttl_costs_no_quota() -> None:
    from carigma_api.services.jobs.quota import RecordedProvider

    db = FakeDB()
    inner = _Inner([_job()])
    provider = RecordedProvider(inner, db, ttl_hours=48)

    first = provider.search(role="Data Analyst", city="Delhi")
    second = provider.search(role="Data Analyst", city="Delhi")

    assert inner.calls == 1, "the second identical search reached the provider"
    assert provider.served_from_cache is True
    assert [j.title for j in second] == [j.title for j in first]
    # Still one call on the cap's books, because there was one call.
    assert fetches_today(db) == 1


def test_a_failed_call_is_not_cached_as_no_jobs() -> None:
    """An empty payload written on a timeout would serve "no jobs" for 48
    hours on the strength of one bad request."""
    from carigma_api.services.jobs.quota import RecordedProvider

    db = FakeDB()
    provider = RecordedProvider(_Inner(fail=True), db, ttl_hours=48)

    try:
        provider.search(role="Data Analyst", city="Delhi")
    except RuntimeError:
        pass
    assert db.rows == []


def test_an_empty_answer_is_cached_because_it_is_an_answer() -> None:
    """ "JSearch has nothing for this today" is worth not asking again."""
    from carigma_api.services.jobs.quota import RecordedProvider

    db = FakeDB()
    inner = _Inner([])
    provider = RecordedProvider(inner, db, ttl_hours=48)

    provider.search(role="Rare Role", city="Delhi")
    provider.search(role="Rare Role", city="Delhi")

    assert inner.calls == 1


def test_a_cached_job_comes_back_whole() -> None:
    from carigma_api.services.jobs.models import ApplyOption, NormalizedJob
    from carigma_api.services.jobs.quota import from_payload, to_payload

    original = NormalizedJob(
        source="jsearch",
        source_id="9",
        title="BI Analyst",
        company="Razorpay",
        location="Bangalore, India",
        apply_url="https://example.com/9",
        apply_options=(ApplyOption(publisher="LinkedIn", url="https://li/9", is_direct=False),),
        salary="₹15-20 LPA",
        posted_at=NOW,
        publisher="LinkedIn",
    )
    rebuilt = from_payload(to_payload(original))

    assert rebuilt is not None
    assert rebuilt.dedupe_key == original.dedupe_key
    assert rebuilt.apply_options == original.apply_options
    assert rebuilt.posted_at == original.posted_at
    assert rebuilt.salary == "₹15-20 LPA"


def test_a_malformed_cache_row_is_skipped_rather_than_shown_half_built() -> None:
    from carigma_api.services.jobs.quota import from_payload

    assert from_payload({"title": "no source, no id"}) is None
