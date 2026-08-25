"""The content loop over real storage.

Round trips throughout — write, then read back through a SEPARATE call.
`/posts/week` returned well-formed payloads for a whole phase while persisting
nothing, so "the handler returned something good-looking" is not evidence.

The test that matters most is `test_two_days_of_one_week_both_survive`. Every
other test here would pass against a week-as-one-blob schema; that one is the
failure the row-per-slot shape was chosen to prevent, and a single-writer round
trip would never show it.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from carigma_api.services.posts import (
    Draft,
    SkipReason,
    Slot,
    SlotState,
    WeekPlan,
    mark_published,
    skip,
)
from carigma_api.services.posts_store import (
    SLOTS,
    SupabasePlanStore,
    slot_from_row,
    slot_to_row,
)

WEEK = date(2026, 8, 17)  # a Monday
MON = Slot(day="MON", slot_date=date(2026, 8, 17))
THU = Slot(day="THU", slot_date=date(2026, 8, 20))

PLAN = WeekPlan(week_start=WEEK, slots=(MON, THU), cadence_per_week=2, streak_weeks=3)


class FakeTable:
    """Enough PostgREST to be wrong in the ways the real one can be."""

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

    def upsert(self, payload: dict[str, Any], **kwargs: Any) -> FakeTable:
        self._op, self._payload = "upsert", payload
        self._conflict = kwargs.get("on_conflict")
        return self

    def eq(self, column: str, value: Any) -> FakeTable:
        self._filters[column] = value
        return self

    def order(self, *_a: Any, **_k: Any) -> FakeTable:
        return self

    def execute(self) -> Any:
        table = self._db.tables.setdefault(self._name, {})
        if self._db.fail_writes and self._op == "upsert":
            raise RuntimeError("write failed")
        if self._op == "upsert":
            assert self._payload is not None
            assert self._conflict, "an upsert with no conflict target creates duplicates"
            # The key is the REAL unique constraint. A fake keyed on anything
            # else would let a duplicate Thursday through, which is the thing
            # the constraint exists to stop.
            key = tuple(self._payload[c] for c in self._conflict.split(","))
            self._db.writes.append((self._name, key))
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
        self.writes: list[tuple[str, tuple[Any, ...]]] = []
        self.fail_writes = False

    def table(self, name: str) -> FakeTable:
        return FakeTable(self, name)


@pytest.fixture
def store() -> tuple[SupabasePlanStore, FakeDB]:
    db = FakeDB()
    return SupabasePlanStore(db), db


# ── The concurrent case the schema exists for ──────────────────────────────


def test_two_days_of_one_week_both_survive(store: tuple[SupabasePlanStore, FakeDB]) -> None:
    """THE test.

    A week stored as one blob makes each edit a read-modify-write of the whole
    week, so a phone marking Monday published and a laptop skipping Thursday
    end with one of them silently erased. Nothing errors; the edit is simply
    gone.

    Both writes are simulated from the SAME starting read — which is what two
    tabs actually have — so a blob schema would fail this and row-per-slot
    passes.
    """
    plan_store, _db = store
    plan_store.save_week("u1", PLAN)

    # Two clients, both holding the week as it was before either edit.
    from_phone = plan_store.load("u1", WEEK)
    from_laptop = plan_store.load("u1", WEEK)
    assert from_phone is not None and from_laptop is not None

    phone_mon = next(s for s in from_phone.slots if s.day == "MON")
    laptop_thu = next(s for s in from_laptop.slots if s.day == "THU")

    plan_store.save_slot("u1", WEEK, mark_published(phone_mon, at="2026-08-17T09:00:00Z"))
    plan_store.save_slot("u1", WEEK, skip(laptop_thu, reason=SkipReason.NO_TIME))

    final = plan_store.load("u1", WEEK)
    assert final is not None
    by_day = {s.day: s for s in final.slots}

    assert by_day["MON"].state is SlotState.PUBLISHED, "the phone's edit was lost"
    assert by_day["THU"].state is SlotState.SKIPPED, "the laptop's edit was lost"
    assert by_day["THU"].skip_reason is SkipReason.NO_TIME


def test_saving_one_slot_writes_exactly_one_row(
    store: tuple[SupabasePlanStore, FakeDB],
) -> None:
    """The mechanism behind the test above. If `save_slot` wrote the week, the
    concurrency test would pass by accident today and break the first time
    someone reordered the calls."""
    plan_store, db = store
    plan_store.save_week("u1", PLAN)
    db.writes.clear()

    plan_store.save_slot("u1", WEEK, mark_published(MON, at="2026-08-17T09:00:00Z"))

    assert len(db.writes) == 1, f"one slot edit touched {len(db.writes)} rows"
    assert db.writes[0][0] == SLOTS


def test_re_saving_a_day_replaces_rather_than_duplicating(
    store: tuple[SupabasePlanStore, FakeDB],
) -> None:
    """`unique (user_id, week_start, day)`. A double-submit must not create a
    second Thursday — two rows for one day would make the week render twice
    and the streak count wrong."""
    plan_store, db = store
    plan_store.save_week("u1", PLAN)

    plan_store.save_slot("u1", WEEK, skip(THU, reason=SkipReason.NO_TIME))
    plan_store.save_slot("u1", WEEK, skip(THU, reason=SkipReason.DRAFTS_WEAK))

    loaded = plan_store.load("u1", WEEK)
    assert loaded is not None
    thursdays = [s for s in loaded.slots if s.day == "THU"]
    assert len(thursdays) == 1
    assert thursdays[0].skip_reason is SkipReason.DRAFTS_WEAK


# ── Round trips ────────────────────────────────────────────────────────────


def test_a_week_round_trips(store: tuple[SupabasePlanStore, FakeDB]) -> None:
    plan_store, _db = store
    plan_store.save_week("u1", PLAN)

    loaded = plan_store.load("u1", WEEK)

    assert loaded is not None
    assert loaded.week_start == WEEK
    assert [s.day for s in loaded.slots] == ["MON", "THU"]
    assert loaded.cadence_per_week == 2
    assert loaded.streak_weeks == 3


def test_every_slot_field_survives_the_round_trip() -> None:
    """The contract store's first draft invented a `label` field and would have
    dropped two real ones. Compared field by field rather than trusting the
    mapping to be complete."""
    rich = Slot(
        day="WED",
        slot_date=date(2026, 8, 19),
        time_label="18:30",
        state=SlotState.DRAFT_READY,
        optional=True,
        drafts=(Draft(index=0, body="first", note="stronger hook"), Draft(index=1, body="second")),
        published_at=None,
        skip_reason=None,
        skip_note=None,
    )

    assert slot_from_row(slot_to_row("u1", WEEK, rich)) == rich


def test_a_skip_reason_round_trips_as_an_enum_not_a_string() -> None:
    skipped = skip(THU, reason=SkipReason.NOT_MY_TOPIC, note="not my area")

    restored = slot_from_row(slot_to_row("u1", WEEK, skipped))

    assert restored.skip_reason is SkipReason.NOT_MY_TOPIC
    assert restored.skip_note == "not my area"


def test_no_skip_reason_stays_none_rather_than_the_string_None() -> None:
    """`str(None)` is `"None"`, which a text column accepts happily and which
    reads back as a skip reason nobody chose."""
    row = slot_to_row("u1", WEEK, MON)

    assert row["skip_reason"] is None
    assert slot_from_row(row).skip_reason is None


def test_slots_come_back_in_date_order(store: tuple[SupabasePlanStore, FakeDB]) -> None:
    """The database returns rows in whatever order it likes; a week renders in
    order."""
    plan_store, _db = store
    plan_store.save_slot("u1", WEEK, THU)
    plan_store.save_slot("u1", WEEK, MON)

    loaded = plan_store.load("u1", WEEK)

    assert loaded is not None
    assert [s.day for s in loaded.slots] == ["MON", "THU"]


# ── Absence ────────────────────────────────────────────────────────────────


def test_an_unplanned_week_is_None_not_an_empty_plan(
    store: tuple[SupabasePlanStore, FakeDB],
) -> None:
    """ "No week yet" and "a week with no slots" are different, and only the
    caller can decide whether to seed a default. Returning one here would make
    them indistinguishable."""
    plan_store, _db = store

    assert plan_store.load("u1", WEEK) is None


def test_one_users_week_is_never_anothers(store: tuple[SupabasePlanStore, FakeDB]) -> None:
    plan_store, _db = store
    plan_store.save_week("u1", PLAN)

    assert plan_store.load("u2", WEEK) is None


def test_a_week_row_missing_its_cadence_falls_back_to_the_default(
    store: tuple[SupabasePlanStore, FakeDB],
) -> None:
    """Slots can exist before `content_weeks` has a row — `save_slot` writes
    only the slot. The week must still load rather than crashing on the gap."""
    plan_store, _db = store
    plan_store.save_slot("u1", WEEK, MON)

    loaded = plan_store.load("u1", WEEK)

    assert loaded is not None
    assert loaded.cadence_per_week >= 1
    assert loaded.cadence_days


def test_a_failed_write_raises_rather_than_reporting_success(
    store: tuple[SupabasePlanStore, FakeDB],
) -> None:
    plan_store, db = store
    db.fail_writes = True

    with pytest.raises(RuntimeError):
        plan_store.save_slot("u1", WEEK, MON)


def test_the_fake_enforces_the_real_unique_key() -> None:
    """Assert the precondition. A fake keyed on anything looser would let a
    duplicate Thursday through and the constraint test above would pass without
    testing the constraint."""
    db = FakeDB()
    store = SupabasePlanStore(db)

    store.save_slot("u1", WEEK, THU)
    store.save_slot("u1", WEEK, THU)

    assert len(db.tables[SLOTS]) == 1


# ── What the port surfaced ─────────────────────────────────────────────────


def test_a_paused_week_survives_having_no_slots(
    store: tuple[SupabasePlanStore, FakeDB],
) -> None:
    """A pause is cadence 0, which means ZERO slots, legitimately.

    Keying existence off the slot rows made "you paused this week" and "you
    have never planned a week" the same answer, so a paused user would be
    handed a fresh default plan and lose the pause. The in-memory version
    stored the `WeekPlan` object whole and never had to decide what an absence
    meant — the port is what asked the question.
    """
    plan_store, _db = store
    paused = WeekPlan(week_start=WEEK, slots=(), cadence_per_week=0, streak_weeks=6)

    plan_store.save_week("u1", paused)
    loaded = plan_store.load("u1", WEEK)

    assert loaded is not None, "a paused week read back as never planned"
    assert loaded.is_paused is True
    assert loaded.streak_weeks == 6


def test_a_cadence_of_zero_is_not_replaced_by_the_default(
    store: tuple[SupabasePlanStore, FakeDB],
) -> None:
    """`week.get("cadence_per_week") or default` promotes every paused week
    back to two posts, because 0 is falsy. The pause is stored correctly and
    read wrongly — the same trap as a credit balance of 0 and a match score of
    0, written fresh into a new file."""
    plan_store, _db = store
    plan_store.save_week("u1", WeekPlan(week_start=WEEK, slots=(), cadence_per_week=0))

    loaded = plan_store.load("u1", WEEK)

    assert loaded is not None
    assert loaded.cadence_per_week == 0


def test_a_week_that_truly_does_not_exist_is_still_None(
    store: tuple[SupabasePlanStore, FakeDB],
) -> None:
    """The other half. Loosening existence must not make every week exist."""
    plan_store, _db = store

    assert plan_store.load("u1", date(2026, 9, 7)) is None
