"""The Sunday sweep, and the store it needs to be meaningful.

`auto_adopt()` was a well-tested pure function with **no caller** for two
phases, so `consecutive_lapses` always returned 0 and the whole lapse escalation was
unreachable. These tests exist because the function being correct was never
the problem.

The store tests are ROUND TRIPS — write, then read back through a separate
call — because shape-verified is not behaviour-verified. `/posts/week` and
`/weekly/contract` both returned well-formed payloads for a whole phase while
persisting nothing.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from carigma_api.services.sweep import GRACE, SweepReport, is_sweepable, sweep
from carigma_api.services.weekly import (
    ContractItem,
    ContractState,
    WeeklyContract,
    consecutive_lapses,
    presentation_for,
)
from carigma_api.services.weekly_store import SupabaseContractStore, from_row, to_row

NOW = datetime(2026, 8, 17, 9, 0, tzinfo=UTC)  # a Monday
LAST_WEEK = date(2026, 8, 3)
THIS_WEEK = date(2026, 8, 17)

ITEMS = (
    ContractItem(kind="content", day="MON", summary="Two posts", minutes=40, cost_credits=0),
    ContractItem(kind="jobs", day="WED", summary="Scan", minutes=10, cost_credits=5),
)


def contract(week: date, state: ContractState = ContractState.PROPOSED) -> WeeklyContract:
    return WeeklyContract(week_start=week, state=state, items=ITEMS, proposed_at=NOW)


class FakeTable:
    def __init__(self, db: FakeDB):
        self._db = db
        self._filters: dict[str, Any] = {}
        self._op: str | None = None
        self._payload: dict[str, Any] | None = None

    def select(self, *_a: Any, **_k: Any) -> FakeTable:
        self._op = "select"
        return self

    def upsert(self, payload: dict[str, Any], **_k: Any) -> FakeTable:
        self._op, self._payload = "upsert", payload
        return self

    def eq(self, column: str, value: Any) -> FakeTable:
        self._filters[column] = value
        return self

    def order(self, *_a: Any, **_k: Any) -> FakeTable:
        return self

    def execute(self) -> Any:
        if self._db.fail_writes and self._op == "upsert":
            raise RuntimeError("write failed")
        if self._op == "upsert":
            assert self._payload is not None
            key = (self._payload["user_id"], self._payload["week_start"])
            # The real table has `unique (user_id, week_start)`, so an upsert
            # REPLACES. A fake that appended would hide a duplicate-row bug.
            self._db.rows[key] = dict(self._payload)
            return type("Res", (), {"data": [dict(self._payload)]})()
        hits = [
            dict(r)
            for r in self._db.rows.values()
            if all(str(r.get(k)) == str(v) for k, v in self._filters.items())
        ]
        return type("Res", (), {"data": hits})()


class FakeDB:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}
        self.fail_writes = False

    def table(self, _name: str) -> FakeTable:
        return FakeTable(self)


# ── The store persists, provably ───────────────────────────────────────────


def test_a_contract_round_trips_through_storage() -> None:
    """Write, then read back through a SEPARATE call.

    Not "the handler returned a good-looking payload" — that is what both
    in-memory surfaces did for a phase while storing nothing.
    """
    store = SupabaseContractStore(FakeDB())
    original = contract(THIS_WEEK, ContractState.APPROVED)

    store.save("u1", original)
    read_back = store.load("u1", THIS_WEEK)

    assert read_back is not None
    assert read_back.state is ContractState.APPROVED
    assert read_back.week_start == THIS_WEEK
    assert [i.kind for i in read_back.items] == ["content", "jobs"]
    assert read_back.items[1].cost_credits == 5


def test_every_item_field_survives_the_round_trip() -> None:
    """The first draft of `to_row` invented a `label` field. `ContractItem` has
    `day`, `summary` and `cost_credits`, so two of them would have been
    silently dropped and one written that nothing reads."""
    restored = from_row(to_row("u1", contract(THIS_WEEK)))

    assert restored.items == ITEMS


def test_re_proposing_a_week_replaces_rather_than_duplicates() -> None:
    """Two contracts for one week would make `consecutive_lapses` walk a history
    that never happened."""
    db = FakeDB()
    store = SupabaseContractStore(db)

    store.save("u1", contract(THIS_WEEK))
    store.save("u1", contract(THIS_WEEK, ContractState.APPROVED))

    assert len(db.rows) == 1
    loaded = store.load("u1", THIS_WEEK)
    assert loaded is not None and loaded.state is ContractState.APPROVED


def test_one_users_history_never_includes_anothers() -> None:
    db = FakeDB()
    store = SupabaseContractStore(db)
    store.save("u1", contract(THIS_WEEK))
    store.save("u2", contract(THIS_WEEK))

    assert len(store.history("u1")) == 1


def test_an_unreadable_timestamp_raises_rather_than_reading_as_undecided() -> None:
    """`decided_at = None` means "never answered", which is what the sweep
    adopts on. Silently degrading a corrupt timestamp to None would make the
    sweep put a plan in force on a week the user already decided."""
    with pytest.raises(ValueError):
        from_row({"week_start": "2026-08-17", "state": "approved", "decided_at": "not-a-timestamp"})


# ── The sweep ──────────────────────────────────────────────────────────────


def test_an_unanswered_week_that_has_ended_is_adopted() -> None:
    db = FakeDB()
    SupabaseContractStore(db).save("u1", contract(LAST_WEEK))

    report = sweep(db, now=NOW)

    assert report.adopted == 1
    stored = SupabaseContractStore(db).load("u1", LAST_WEEK)
    assert stored is not None
    assert stored.state is ContractState.AUTO_ADOPTED


def test_the_current_week_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sweeping the week it was proposed adopts a plan the user still has all
    week to answer."""
    db = FakeDB()
    SupabaseContractStore(db).save("u1", contract(THIS_WEEK))

    report = sweep(db, now=NOW)

    assert report.adopted == 0
    assert report.too_recent == 1


def test_the_grace_boundary_is_the_end_of_the_week_not_the_start() -> None:
    proposed = contract(LAST_WEEK)

    assert is_sweepable(proposed, today=LAST_WEEK) is False
    assert is_sweepable(proposed, today=LAST_WEEK + GRACE - timedelta(days=1)) is False
    assert is_sweepable(proposed, today=LAST_WEEK + GRACE) is True


def test_a_decided_week_is_never_re_adopted() -> None:
    """Adopting an approved week would overwrite what the user actually chose
    with the conservative plan."""
    for decided in (ContractState.APPROVED, ContractState.DECLINED):
        assert is_sweepable(contract(LAST_WEEK, decided), today=THIS_WEEK) is False


# ── The two conditions ─────────────────────────────────────────────────────


def test_what_carries_forward_is_conservative() -> None:
    """Condition one. An unanswered week continues at the smaller commitment,
    not the one the user never agreed to."""
    db = FakeDB()
    SupabaseContractStore(db).save("u1", contract(LAST_WEEK))

    sweep(db, now=NOW)

    stored = SupabaseContractStore(db).load("u1", LAST_WEEK)
    assert stored is not None
    assert stored.total_minutes <= sum(i.minutes for i in ITEMS)


def test_an_adopted_week_is_never_reported_as_agreed() -> None:
    """Condition two, structurally. No surface can render this as approval
    because the type refuses to say so."""
    db = FakeDB()
    SupabaseContractStore(db).save("u1", contract(LAST_WEEK))

    sweep(db, now=NOW)
    stored = SupabaseContractStore(db).load("u1", LAST_WEEK)

    assert stored is not None
    assert stored.state.is_decided is True
    assert stored.state.user_actually_agreed is False


def test_an_adopted_week_accepted_nothing() -> None:
    """`accepted_kinds` stays empty, because the user accepted nothing."""
    db = FakeDB()
    SupabaseContractStore(db).save("u1", contract(LAST_WEEK))

    sweep(db, now=NOW)
    stored = SupabaseContractStore(db).load("u1", LAST_WEEK)

    assert stored is not None
    assert stored.accepted_kinds == ()


# ── Observability ──────────────────────────────────────────────────────────


def test_the_sweep_names_what_it_carried_rather_than_counting() -> None:
    """ "3 adopted" cannot be checked against anything. This is the first thing
    that ever writes `auto_adopted`, so the evidence it worked has to be
    legible."""
    db = FakeDB()
    SupabaseContractStore(db).save("u1", contract(LAST_WEEK))

    report = sweep(db, now=NOW)

    assert report.carried, "the sweep adopted a week and recorded nothing about it"
    user_id, week, kinds = report.carried[0]
    assert user_id == "u1"
    assert week == LAST_WEEK.isoformat()
    assert kinds, "no item kinds recorded"


def test_a_failed_save_is_not_reported_as_an_adoption() -> None:
    """A report claiming work that did not land is the sweep lying about
    itself."""
    db = FakeDB()
    SupabaseContractStore(db).save("u1", contract(LAST_WEEK))
    db.fail_writes = True

    report = sweep(db, now=NOW)

    assert report.adopted == 0
    assert report.failed == 1
    assert report.carried == []


def test_a_dry_run_changes_nothing() -> None:
    db = FakeDB()
    SupabaseContractStore(db).save("u1", contract(LAST_WEEK))

    report = sweep(db, now=NOW, dry_run=True)

    assert report.adopted == 1
    stored = SupabaseContractStore(db).load("u1", LAST_WEEK)
    assert stored is not None and stored.state is ContractState.PROPOSED


def test_the_report_is_serialisable_for_the_admin_surface() -> None:
    report = SweepReport(adopted=2, carried=[("u1", "2026-08-03", ["content"])])

    payload = report.as_dict()

    assert payload["adopted"] == 2
    assert payload["carried"][0]["kinds"] == ["content"]


# ── And the escalation it unblocks ─────────────────────────────────────────


def test_the_lapse_escalation_can_now_actually_fire() -> None:
    """The point of the whole exercise. Before the sweep existed, nothing wrote
    `auto_adopted`, so this always returned 0 and `presentation_for` always
    returned "none"."""
    history = [
        contract(LAST_WEEK - timedelta(weeks=1), ContractState.AUTO_ADOPTED),
        contract(LAST_WEEK, ContractState.AUTO_ADOPTED),
    ]

    lapses = consecutive_lapses(history, upto=THIS_WEEK)

    assert lapses == 2
    assert presentation_for(lapses) == "standing_by"


def test_an_approved_week_stops_the_lapse_count() -> None:
    """A decision is not a lapse, however long ago."""
    history = [
        contract(LAST_WEEK - timedelta(weeks=1), ContractState.APPROVED),
        contract(LAST_WEEK, ContractState.AUTO_ADOPTED),
    ]

    assert consecutive_lapses(history, upto=THIS_WEEK) == 1


# ── The surface copy: the user-facing half of the decision ─────────────────


def test_an_adopted_week_says_plainly_it_was_not_agreed() -> None:
    """The state refuses to claim agreement. The sentence has to match it."""
    from carigma_api.services.weekly import auto_adopt, carried_forward_copy

    copy = carried_forward_copy(auto_adopt(contract(LAST_WEEK), now=NOW))

    assert copy is not None
    assert copy["agreed"] is False
    assert "haven't chosen" in copy["body"]


def test_the_copy_offers_approve_and_change_both():  # type: ignore[no-untyped-def]
    """An honest statement with nowhere to go is the dead end this codebase
    keeps deleting."""
    from carigma_api.services.weekly import auto_adopt, carried_forward_copy

    copy = carried_forward_copy(auto_adopt(contract(LAST_WEEK), now=NOW))

    assert copy is not None
    labels = " ".join(a["label"] for a in copy["actions"]).lower()
    assert "approve" in labels
    assert "change" in labels


def test_the_copy_names_what_actually_carried() -> None:
    """Built from the adopted items, so it cannot claim something that did not
    continue. `content` pauses under the conservative plan, so it must not
    appear."""
    from carigma_api.services.weekly import auto_adopt, carried_forward_copy

    copy = carried_forward_copy(auto_adopt(contract(LAST_WEEK), now=NOW))

    assert copy is not None
    assert "jobs" in copy["carried_kinds"]
    assert "content" not in copy["carried_kinds"], "post drafts pause; the copy said otherwise"
    for kind in copy["carried_kinds"]:
        assert kind in copy["body"]


def test_the_copy_does_not_reprimand() -> None:
    """A week not answered is a week someone was busy."""
    from carigma_api.services.weekly import auto_adopt, carried_forward_copy

    copy = carried_forward_copy(auto_adopt(contract(LAST_WEEK), now=NOW))

    assert copy is not None
    blob = f"{copy['headline']} {copy['body']}".lower()
    for banned in ("you missed", "don't lose", "falling behind", "inactive", "should have"):
        assert banned not in blob


def test_the_guilt_check_runs_on_the_ASSEMBLED_sentence() -> None:
    """Not on literals. `carried_kinds` comes from data, so a guard over source
    strings would never see it."""
    from carigma_api.services.weekly import GuiltyCopy, carried_forward_copy

    guilty = WeeklyContract(
        week_start=LAST_WEEK,
        state=ContractState.AUTO_ADOPTED,
        # An actual entry from BANNED_PHRASES, read rather than invented. The
        # first version used "you missed the deadline", which is not on the
        # list — so the test asserted the guard catches a string the guard was
        # never asked about, and the failure was mine, not the guard's.
        items=(ContractItem(kind="falling behind", day="MON", summary="x", minutes=1),),
    )

    with pytest.raises(GuiltyCopy):
        carried_forward_copy(guilty)


def test_a_week_the_user_actually_decided_gets_no_carried_forward_notice() -> None:
    """Telling someone their approved plan "carried forward" would erase the
    fact that they chose it."""
    from carigma_api.services.weekly import carried_forward_copy

    for decided in (ContractState.APPROVED, ContractState.DECLINED, ContractState.PROPOSED):
        assert carried_forward_copy(contract(LAST_WEEK, decided)) is None


# ── The admin signal ───────────────────────────────────────────────────────


def _admin_signal(rows: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    from carigma_api.routes import market as market_routes

    class Stub:
        def table(self, _name: str) -> Any:
            outer = self

            class T:
                def select(self, *_a: Any, **_k: Any) -> Any:
                    return self

                def execute(self) -> Any:
                    return type("Res", (), {"data": [dict(r) for r in outer.rows]})()

            return T()

    stub = Stub()
    stub.rows = rows  # type: ignore[attr-defined]
    monkeypatch.setattr(market_routes, "service_client", lambda settings: stub)
    return market_routes.weekly_contract_signal(admin=None, settings=None)  # type: ignore[arg-type]


def test_the_admin_signal_counts_adopted_against_approved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _admin_signal(
        [{"state": "approved"}, {"state": "auto_adopted"}, {"state": "auto_adopted"}],
        monkeypatch,
    )

    assert payload["approved"] == 1
    assert payload["auto_adopted"] == 2
    assert payload["ratio_adopted_to_approved"] == 2.0


def test_no_approvals_yet_is_null_not_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """ "Nobody has approved anything" and "one adoption per approval" are
    different facts. A zero here would read as the second."""
    payload = _admin_signal([{"state": "auto_adopted"}], monkeypatch)

    assert payload["ratio_adopted_to_approved"] is None


def test_the_signal_reports_every_state_the_enum_has(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Derived from `ContractState`, so a new state cannot be silently absent
    from the dashboard."""
    payload = _admin_signal([{"state": "approved"}], monkeypatch)

    assert set(payload["counts"]) == {str(s) for s in ContractState}


def test_the_signal_does_not_call_auto_adoption_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It is a legitimate outcome the design allows. The note must point at
    when the week is proposed, not at the user."""
    note = _admin_signal([{"state": "auto_adopted"}], monkeypatch)["note"].lower()

    assert "legitimate" in note
    for blaming in ("ignored", "failed to", "neglect", "did not bother"):
        assert blaming not in note
