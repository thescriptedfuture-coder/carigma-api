"""The shared JSearch quota, and the three times it was not enforced.

`config.py` has said since the beginning that the JSearch quota is "handled by
the jobs cache + daily cap". S5 built a cap nothing enforced; S6 enforced a cap
nothing recorded into; 0.063 recorded into a table the user's client could not
see — the database answered every read with zero rows and refused every write,
and zero rows is not an error, so the cap read 0 through three real scans.

The fakes in the earlier versions of this file let a user-scoped client read
and write `jobs_cache` freely. Production does not. That is why every test here
passed while the cap did nothing: **a fake that permits what the database
refuses tests a system that does not exist.** The static half of that lesson is
`test_rls_probe.py::test_no_user_path_names_a_service_only_table`; the live
half is `scripts/verify_cap.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from carigma_api.services.jobs.quota import (
    LEDGER,
    CallNotRecorded,
    RecordedProvider,
    calls_today,
    query_key,
    refusal,
)

NOW = datetime(2026, 10, 15, 14, 0, tzinfo=UTC)
CAP = 25


class _Rpc:
    def __init__(self, db: FakeDB) -> None:
        self._db = db

    def execute(self) -> Any:
        if self._db.counter_fails:
            raise RuntimeError("function jsearch_calls_today() does not exist")
        return type("Res", (), {"data": self._db.counter})()


class _Table:
    def __init__(self, db: FakeDB, name: str) -> None:
        self._db = db
        self._name = name
        self._payload: Any = None

    def insert(self, payload: Any) -> _Table:
        self._payload = payload
        return self

    def execute(self) -> Any:
        if self._db.ledger_refuses:
            raise RuntimeError("new row violates row-level security policy (42501)")
        self._db.written.append((self._name, dict(self._payload)))
        # The call log is what the counter adds up; keep the two honest with
        # each other so a test can watch a call become a count.
        if self._name == LEDGER and self._payload.get("source") == "jsearch":
            self._db.counter = (self._db.counter or 0) + 1
        return type("Res", (), {"data": [dict(self._payload)]})()


class FakeDB:
    def __init__(self, counter: Any = 0) -> None:
        self.counter = counter
        self.counter_fails = False
        self.ledger_refuses = False
        self.written: list[tuple[str, dict[str, Any]]] = []
        self.rpc_names: list[str] = []

    def rpc(self, name: str, _params: dict[str, Any] | None = None) -> _Rpc:
        self.rpc_names.append(name)
        return _Rpc(self)

    def table(self, name: str) -> _Table:
        return _Table(self, name)


# ── Cannot count is not counted zero ───────────────────────────────────────


def test_the_count_comes_from_the_counter_function() -> None:
    db = FakeDB(counter=7)
    assert calls_today(db) == 7
    assert db.rpc_names == ["jsearch_calls_today"]


def test_a_counter_that_cannot_be_called_is_none_and_none_refuses() -> None:
    """Until V2_018 is applied the function does not exist. That must stop
    every scan, not wave them through."""
    db = FakeDB()
    db.counter_fails = True

    assert calls_today(db) is None
    message = refusal(db, cap=CAP, now=NOW)
    assert message is not None
    assert "couldn't check today's search quota" in message
    assert "Nothing was charged" in message


@pytest.mark.parametrize("shape", [None, "3", 3.0, {"count": 3}, [], [1, 2], True, False])
def test_anything_but_a_plain_integer_is_not_a_count(shape: Any) -> None:
    """`True` is an `int` in Python and would count as one call; `None`, a
    string and an empty list would each read as "nothing used" somewhere."""
    assert calls_today(FakeDB(counter=shape)) is None


def test_a_single_element_list_is_read_as_its_value() -> None:
    assert calls_today(FakeDB(counter=[4])) == 4


def test_no_calls_today_is_a_real_zero_and_allows_the_scan() -> None:
    assert refusal(FakeDB(counter=0), cap=CAP, now=NOW) is None


def test_the_cap_refuses_at_the_limit_not_past_it() -> None:
    message = refusal(FakeDB(counter=CAP), cap=CAP, now=NOW)

    assert message is not None
    assert f"{CAP} of {CAP}" in message
    assert "Nothing was charged" in message


def test_the_reset_is_said_as_a_wait_not_a_utc_clock() -> None:
    """14:00 UTC is ten hours from midnight UTC. "Resets at midnight UTC" is
    05:30 for most readers and a sum they should not have to do."""
    message = refusal(FakeDB(counter=CAP), cap=CAP, now=NOW)

    assert message is not None
    assert "about 10 hours" in message
    assert "UTC" not in message


def test_one_below_the_cap_still_goes() -> None:
    assert refusal(FakeDB(counter=CAP - 1), cap=CAP, now=NOW) is None


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
    """`Business Analyst — ERP & Public Finance / MoD` is a real stored title."""
    key = query_key(
        role="Business Analyst — ERP & Public Finance / MoD", city="New Delhi", source="jsearch"
    )
    assert key.startswith("jsearch:")
    assert len(key) < 120


# ── Counted at the call ────────────────────────────────────────────────────


class _Inner:
    """A provider that counts how often it is really called, and when."""

    name = "jsearch"

    def __init__(self, db: FakeDB, jobs: list[Any] | None = None, fail: bool = False) -> None:
        self._db = db
        self.calls = 0
        self.jobs = jobs or []
        self.fail = fail
        #: How many rows the ledger held at the moment of each call.
        self.ledger_at_call: list[int] = []

    def available(self) -> bool:
        return True

    def search(self, *, role: str, city: str, limit: int = 20) -> list[Any]:
        self.calls += 1
        self.ledger_at_call.append(len(self._db.written))
        if self.fail:
            raise RuntimeError("provider down")
        return list(self.jobs)


def test_a_real_call_leaves_a_row_the_cap_counts() -> None:
    """The wiring, which is what was missing twice.

    Asserted through the count — the number the cap reads — rather than
    through the row alone, because "a row was written" passed in 0.063 while
    the count stayed at zero.
    """
    db = FakeDB(counter=0)
    inner = _Inner(db)
    provider = RecordedProvider(inner, db, user_id="u1")

    provider.search(role="Data Analyst", city="Delhi")

    assert inner.calls == 1
    assert calls_today(db) == 1
    table, row = db.written[0]
    assert table == LEDGER
    assert row == {
        "user_id": "u1",
        "source": "jsearch",
        "query_key": query_key(role="Data Analyst", city="Delhi", source="jsearch"),
    }


def test_the_row_is_written_before_the_call_is_made() -> None:
    """After the call, a write that fails would leave quota spent that the
    cap never sees."""
    db = FakeDB()
    inner = _Inner(db)

    RecordedProvider(inner, db, user_id="u1").search(role="Data Analyst", city="Delhi")

    assert inner.ledger_at_call == [1], "the provider was reached before the call was recorded"


def test_no_row_means_no_call() -> None:
    """THE property. A refused ledger write is exactly what production did
    for three scans, silently. Now it stops the call."""
    db = FakeDB()
    db.ledger_refuses = True
    inner = _Inner(db)

    with pytest.raises(CallNotRecorded):
        RecordedProvider(inner, db, user_id="u1").search(role="Data Analyst", city="Delhi")

    assert inner.calls == 0


def test_a_call_that_fails_after_recording_still_counts() -> None:
    """A timeout may still have spent quota. Over-counting is the safe
    direction when the quota belongs to somebody else."""
    db = FakeDB(counter=0)
    inner = _Inner(db, fail=True)

    with pytest.raises(RuntimeError):
        RecordedProvider(inner, db, user_id="u1").search(role="Data Analyst", city="Delhi")

    assert calls_today(db) == 1


def test_a_refused_record_is_a_provider_the_gateway_skips_not_a_crash() -> None:
    """`CallNotRecorded` is a `ProviderUnavailable`, so the gateway notes it
    and moves on — and a scan where nothing could be recorded ends as
    "nobody answered", which is free."""
    from carigma_api.services.jobs.adapter import JobsGateway, ProviderUnavailable

    assert issubclass(CallNotRecorded, ProviderUnavailable)

    db = FakeDB()
    db.ledger_refuses = True
    inner = _Inner(db)
    found, notes = JobsGateway([RecordedProvider(inner, db, user_id="u1")]).search(
        role="Data Analyst", city="Delhi"
    )

    assert found == []
    assert notes == ["jsearch: unavailable"]
    assert inner.calls == 0


def test_every_provider_is_recorded_not_only_the_capped_one() -> None:
    """Adzuna is not what the cap counts, but a call log with holes in it is
    not a call log."""
    db = FakeDB(counter=0)

    class _Adzuna(_Inner):
        name = "adzuna"

    RecordedProvider(_Adzuna(db), db, user_id="u1").search(role="Data Analyst", city="Delhi")

    assert [row["source"] for _t, row in db.written] == ["adzuna"]
    assert calls_today(db) == 0, "an Adzuna call counted against the JSearch cap"
