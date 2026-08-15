"""Thread decisions, round-tripped.

A snooze that does not survive the next request is a rung that reappears after
being dismissed — the user told us something and we lost it, on the surface
where they told us. So every test here writes and then reads back through a
SEPARATE call, rather than asserting the handler returned something plausible.

That distinction is not theoretical: `/thread` returned a well-formed payload
for a whole phase while persisting nothing at all.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from carigma_api.services.thread_store import (
    TABLE,
    Decision,
    SupabaseThreadStore,
    is_suppressed,
    surviving,
)

NOW = datetime(2026, 8, 17, 9, 0, tzinfo=UTC)


class FakeTable:
    def __init__(self, db: FakeDB, name: str):
        self._db = db
        self._name = name
        self._filters: dict[str, Any] = {}
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

    def execute(self) -> Any:
        table = self._db.tables.setdefault(self._name, {})
        if self._op == "upsert":
            assert self._payload is not None
            # Keyed on the REAL unique constraint from V2_001,
            # `unique (user_id, item_key)`. A looser key would let two
            # decisions about one item coexist, which is the bug the
            # constraint prevents.
            assert self._conflict == "user_id,item_key", "upsert must target the real constraint"
            key = tuple(self._payload[c] for c in self._conflict.split(","))
            table[key] = dict(self._payload)
            return type("Res", (), {"data": [dict(self._payload)]})()
        hits = [
            dict(r)
            for r in table.values()
            if all(str(r.get(k)) == str(v) for k, v in self._filters.items())
        ]
        return type("Res", (), {"data": hits})()


class FakeDB:
    def __init__(self) -> None:
        self.tables: dict[str, dict[tuple[Any, ...], dict[str, Any]]] = {}

    def table(self, name: str) -> FakeTable:
        return FakeTable(self, name)


def store() -> tuple[SupabaseThreadStore, FakeDB]:
    db = FakeDB()
    return SupabaseThreadStore(db), db


# ── The round trip ─────────────────────────────────────────────────────────


def test_a_dismissal_survives_a_separate_read() -> None:
    """The whole point. An item that comes back after being dismissed is worse
    than one that was never dismissible."""
    thread, _db = store()

    thread.record("u1", "post_day", Decision.DISMISSED)
    back = thread.decisions("u1")

    assert back["post_day"]["decision"] == "dismissed"
    assert is_suppressed(back["post_day"], now=NOW) is True


def test_a_snooze_survives_with_its_deadline() -> None:
    thread, _db = store()
    until = NOW + timedelta(days=2)

    thread.record("u1", "new_matches", Decision.SNOOZED, snooze_until=until)
    back = thread.decisions("u1")

    assert back["new_matches"]["decision"] == "snoozed"
    assert back["new_matches"]["snooze_until"] == until.isoformat()
    assert is_suppressed(back["new_matches"], now=NOW) is True


def test_deciding_twice_replaces_rather_than_stacking() -> None:
    """A snooze followed by a dismiss ends as a dismiss. Two rows for one item
    would make the outcome depend on which the reader saw first."""
    thread, db = store()

    thread.record("u1", "score_fix", Decision.SNOOZED, snooze_until=NOW + timedelta(days=1))
    thread.record("u1", "score_fix", Decision.DISMISSED)

    assert len(db.tables[TABLE]) == 1
    assert thread.decisions("u1")["score_fix"]["decision"] == "dismissed"


def test_one_users_decisions_are_never_anothers() -> None:
    thread, _db = store()
    thread.record("u1", "post_day", Decision.DISMISSED)

    assert thread.decisions("u2") == {}


# ── A snooze is a time, not a flag ─────────────────────────────────────────


def test_a_snooze_stops_applying_on_its_own() -> None:
    """Otherwise "snooze" is a slower dismiss, which is not what the user
    chose."""
    thread, _db = store()
    thread.record("u1", "new_matches", Decision.SNOOZED, snooze_until=NOW + timedelta(hours=1))
    row = thread.decisions("u1")["new_matches"]

    assert is_suppressed(row, now=NOW) is True
    assert is_suppressed(row, now=NOW + timedelta(hours=2)) is False


def test_a_dismissal_never_expires() -> None:
    thread, _db = store()
    thread.record("u1", "post_day", Decision.DISMISSED)
    row = thread.decisions("u1")["post_day"]

    assert is_suppressed(row, now=NOW + timedelta(days=3650)) is True


def test_a_snooze_with_no_deadline_shows_the_item_rather_than_swallowing_it() -> None:
    """A snooze with no end is a state nothing can leave. Erring toward showing
    someone their own item back is recoverable; the other way silently drops it
    forever."""
    corrupt = {"decision": "snoozed", "snooze_until": None, "user_id": "u1"}

    assert is_suppressed(corrupt, now=NOW) is False


def test_an_unparseable_deadline_does_not_hide_the_item_forever() -> None:
    corrupt = {"decision": "snoozed", "snooze_until": "not-a-time"}

    assert is_suppressed(corrupt, now=NOW) is False


def test_an_undecided_item_is_not_suppressed() -> None:
    """The common case: no row at all."""
    assert is_suppressed({}, now=NOW) is False


# ── Filtering the derived items ────────────────────────────────────────────


def test_surviving_drops_only_what_was_decided() -> None:
    """Candidates are DERIVED every load; only the decisions are stored. This
    is where the two meet."""
    thread, _db = store()
    thread.record("u1", "post_day", Decision.DISMISSED)
    thread.record("u1", "new_matches", Decision.SNOOZED, snooze_until=NOW + timedelta(days=1))

    items = [{"key": "post_day"}, {"key": "score_fix"}, {"key": "new_matches"}]
    kept = surviving(items, thread.decisions("u1"), now=NOW)

    assert [i["key"] for i in kept] == ["score_fix"]


def test_an_expired_snooze_lets_its_item_return() -> None:
    thread, _db = store()
    thread.record("u1", "new_matches", Decision.SNOOZED, snooze_until=NOW + timedelta(hours=1))

    later = surviving([{"key": "new_matches"}], thread.decisions("u1"), now=NOW + timedelta(days=1))

    assert [i["key"] for i in later] == ["new_matches"]


def test_nothing_decided_means_nothing_filtered() -> None:
    items = [{"key": "a"}, {"key": "b"}]

    assert surviving(items, {}, now=NOW) == items
