"""The pipeline over HTTP. Spec 3.5.

Fixtures carry what `job_applications` actually holds: thirteen rows for one
user, a legacy `rejected` among them, `interview_on` as a DATE, and a `closed`
row that still has an interview date in it.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from carigma_api.routes import tracker as routes
from tests.conftest import USER_ID, auth, make_token

NOW = datetime.now(UTC)


class FakeTable:
    def __init__(self, db: FakeDB, name: str) -> None:
        self._db, self._name = db, name
        self._filters: dict[str, Any] = {}
        self._op: str | None = None
        self._payload: dict[str, Any] | None = None
        self._columns: list[str] | None = None

    def select(self, *columns: Any, **_k: Any) -> FakeTable:
        self._op = "select"
        # Honour the projection. The first version returned whole rows, so the
        # timeline came back with `id`, `user_id` and `application_id` that the
        # real query never selects — and the recorded contract said the API
        # sends them. A fake more generous than production is how a surface
        # comes to read a field that is not there.
        wanted = ",".join(str(c) for c in columns if c)
        self._columns = None if not wanted or wanted == "*" else wanted.split(",")
        return self

    def insert(self, payload: dict[str, Any]) -> FakeTable:
        self._op, self._payload = "insert", payload
        return self

    def update(self, payload: dict[str, Any]) -> FakeTable:
        self._op, self._payload = "update", payload
        return self

    def delete(self) -> FakeTable:
        self._op = "delete"
        return self

    def eq(self, column: str, value: Any) -> FakeTable:
        self._filters[column] = value
        return self

    def order(self, *_a: Any, **_k: Any) -> FakeTable:
        return self

    def limit(self, _n: int) -> FakeTable:
        return self

    def _hits(self) -> list[tuple[Any, dict[str, Any]]]:
        table = self._db.tables.setdefault(self._name, {})
        return [
            (key, row)
            for key, row in table.items()
            if all(str(row.get(c)) == str(v) for c, v in self._filters.items())
        ]

    def execute(self) -> Any:
        if self._name in self._db.failing:
            raise RuntimeError(f"{self._name} is down")
        table = self._db.tables.setdefault(self._name, {})
        if self._op == "insert":
            assert self._payload is not None
            row = {"id": self._payload.get("id") or f"app-{len(table) + 1}", **self._payload}
            table[row["id"]] = row
            self._db.inserted.append((self._name, dict(row)))
            return type("Res", (), {"data": [dict(row)]})()
        if self._op == "update":
            assert self._payload is not None
            for key, row in self._hits():
                table[key] = {**row, **self._payload}
            return type("Res", (), {"data": []})()
        if self._op == "delete":
            for key, _row in self._hits():
                table.pop(key, None)
            return type("Res", (), {"data": []})()
        rows = [dict(r) for _k, r in self._hits()]
        if self._columns is not None:
            rows = [{c: row.get(c) for c in self._columns} for row in rows]
        return type("Res", (), {"data": rows})()


class FakeDB:
    def __init__(self) -> None:
        self.tables: dict[str, dict[Any, dict[str, Any]]] = {}
        self.failing: set[str] = set()
        self.inserted: list[tuple[str, dict[str, Any]]] = []

    def table(self, name: str) -> FakeTable:
        return FakeTable(self, name)

    def seed(self, name: str, row: dict[str, Any]) -> dict[str, Any]:
        self.tables.setdefault(name, {})[row["id"]] = row
        return row


@pytest.fixture
def wired(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    db = FakeDB()
    monkeypatch.setattr(routes, "_db", lambda request, settings: db)
    yield client, db


def token() -> dict[str, str]:
    return auth(make_token())


def application(**kw: Any) -> dict[str, Any]:
    row = {
        "id": "app-1",
        "user_id": USER_ID,
        "company": "Zomato",
        "role": "Data Analyst",
        "status": "applied",
        "link": None,
        "salary": None,
        "notes": None,
        "interview_on": None,
        "closed_reason": None,
        "created_at": (NOW - timedelta(days=3)).isoformat(),
        "updated_at": (NOW - timedelta(days=3)).isoformat(),
    }
    row.update(kw)
    return row


# ── Auth ───────────────────────────────────────────────────────────────────


def test_every_endpoint_requires_a_verified_token(client: TestClient) -> None:
    assert client.get("/tracker").status_code == 401
    assert client.get("/tracker/app-1").status_code == 401
    assert client.post("/tracker", json={"company": "X", "role": "Y"}).status_code == 401
    assert client.patch("/tracker/app-1", json={"stage": "applied"}).status_code == 401
    assert client.delete("/tracker/app-1").status_code == 401


# ── The board ──────────────────────────────────────────────────────────────


def test_every_stage_is_a_column_even_when_it_is_empty(wired: Any) -> None:
    """A board that hides its empty columns teaches a different shape of the
    pipeline every time it is opened."""
    client, _db = wired
    body = client.get("/tracker", headers=token()).json()

    assert [c["stage"] for c in body["columns"]] == [
        "saved",
        "applied",
        "interview",
        "offer",
        "closed",
    ]
    assert all(c["count"] == 0 for c in body["columns"])
    assert body["summary"] == {"total": 0, "active": 0, "interviewing": 0, "needs_follow_up": 0}


def test_a_legacy_rejected_row_lands_in_closed(wired: Any) -> None:
    """One live row still says `rejected`, which predates the closed + reason
    split. Dropping it would lose an application from the board entirely."""
    client, db = wired
    db.seed("job_applications", application(id="a", status="rejected"))

    body = client.get("/tracker", headers=token()).json()
    closed = next(c for c in body["columns"] if c["stage"] == "closed")

    assert closed["count"] == 1
    assert closed["items"][0]["stage_label"] == "Closed"


def test_the_summary_counts_live_applications_not_bookmarks(wired: Any) -> None:
    client, db = wired
    for i, status in enumerate(("saved", "applied", "interview", "offer", "closed")):
        db.seed("job_applications", application(id=f"a{i}", status=status))

    body = client.get("/tracker", headers=token()).json()

    assert body["summary"]["total"] == 5
    assert body["summary"]["active"] == 2
    assert body["summary"]["interviewing"] == 1


def test_ten_days_in_applied_asks_for_a_follow_up(wired: Any) -> None:
    """V1's rule and V1's threshold, ported rather than picked."""
    client, db = wired
    stale = (NOW - timedelta(days=11)).isoformat()
    db.seed("job_applications", application(id="a", status="applied", updated_at=stale))
    db.seed("job_applications", application(id="b", status="applied"))
    # Eleven days in a stage that is not `applied` is not a follow-up.
    db.seed("job_applications", application(id="c", status="saved", updated_at=stale))

    body = client.get("/tracker", headers=token()).json()
    applied = next(c for c in body["columns"] if c["stage"] == "applied")

    assert body["summary"]["needs_follow_up"] == 1
    assert [i["needs_follow_up"] for i in applied["items"]] == [True, False]


def test_a_broken_read_is_a_503_not_an_empty_board(wired: Any) -> None:
    """An empty pipeline and an unreadable one look identical on a kanban."""
    client, db = wired
    db.failing.add("job_applications")

    res = client.get("/tracker", headers=token())
    assert res.status_code == 503
    assert res.json()["detail"]["error"] == "read_failed"


# ── One application, and its timeline ──────────────────────────────────────


def test_the_timeline_distinguishes_empty_from_unreadable(wired: Any) -> None:
    client, db = wired
    db.seed("job_applications", application(id="a"))

    body = client.get("/tracker/a", headers=token()).json()
    assert body["events"] == []

    db.failing.add("application_events")
    body = client.get("/tracker/a", headers=token()).json()
    # `None`, not `[]`. The row still renders; the history is unknown.
    assert body["events"] is None
    assert body["company"] == "Zomato"


def test_another_users_application_is_a_404(wired: Any) -> None:
    client, db = wired
    db.seed("job_applications", application(id="a", user_id="someone-else"))

    assert client.get("/tracker/a", headers=token()).status_code == 404


# ── Adding ─────────────────────────────────────────────────────────────────


def test_adding_lands_in_saved_and_writes_one_event(wired: Any) -> None:
    client, db = wired
    res = client.post(
        "/tracker",
        json={"company": "Razorpay", "role": "Business Analyst"},
        headers=token(),
    )

    assert res.status_code == 201
    assert res.json()["stage"] == "saved"
    events = [row for table, row in db.inserted if table == "application_events"]
    assert [e["event_type"] for e in events] == ["stage_saved"]


def test_an_unknown_stage_on_add_is_a_bookmark_not_a_crash(wired: Any) -> None:
    """`normalise_stage` sends anything unrecognised to `saved`. Never silently
    'live' — a bad value must not inflate the pipeline count."""
    client, _db = wired
    body = client.post(
        "/tracker",
        json={"company": "X", "role": "Y", "stage": "screening"},
        headers=token(),
    ).json()
    assert body["stage"] == "saved"


# ── Moving ─────────────────────────────────────────────────────────────────


def test_a_move_writes_the_stage_and_an_event(wired: Any) -> None:
    client, db = wired
    db.seed("job_applications", application(id="a", status="applied"))

    body = client.patch("/tracker/a", json={"stage": "interview"}, headers=token()).json()

    assert body["stage"] == "interview"
    events = [row for table, row in db.inserted if table == "application_events"]
    assert [e["event_type"] for e in events] == ["stage_interview"]


def test_moving_to_the_stage_it_already_has_writes_no_event(wired: Any) -> None:
    """A timeline that says "moved to Applied" five times is one nobody reads."""
    client, db = wired
    db.seed("job_applications", application(id="a", status="applied"))

    client.patch("/tracker/a", json={"stage": "applied"}, headers=token())

    assert [row for table, row in db.inserted if table == "application_events"] == []


def test_closing_carries_its_reason_into_the_timeline(wired: Any) -> None:
    client, db = wired
    db.seed("job_applications", application(id="a", status="applied"))

    body = client.patch(
        "/tracker/a",
        json={"stage": "closed", "closed_reason": "Ghosted"},
        headers=token(),
    ).json()

    assert body["stage"] == "closed"
    assert body["closed_reason"] == "Ghosted"
    events = [row for table, row in db.inserted if table == "application_events"]
    assert events[0]["event_type"] == "stage_closed"
    assert events[0]["note"] == "Closed — Ghosted"


def test_a_reason_we_do_not_offer_is_refused(wired: Any) -> None:
    client, db = wired
    db.seed("job_applications", application(id="a"))

    res = client.patch(
        "/tracker/a", json={"stage": "closed", "closed_reason": "Vibes"}, headers=token()
    )
    assert res.status_code == 422
    assert res.json()["detail"]["error"] == "unknown_reason"


def test_an_interview_date_is_stored_as_a_date(wired: Any) -> None:
    """The column is a DATE. Storing a timestamp is how the comparison in
    `next_interview` started raising on an aware `now`."""
    client, db = wired
    db.seed("job_applications", application(id="a"))

    client.patch(
        "/tracker/a",
        json={"stage": "interview", "interview_on": "2026-11-04T10:30:00Z"},
        headers=token(),
    )

    assert db.tables["job_applications"]["a"]["interview_on"] == "2026-11-04"


def test_a_date_we_cannot_read_is_refused_rather_than_stored(wired: Any) -> None:
    client, db = wired
    db.seed("job_applications", application(id="a"))

    res = client.patch("/tracker/a", json={"interview_on": "next tuesday"}, headers=token())
    assert res.status_code == 422
    assert res.json()["detail"]["error"] == "bad_date"


def test_a_patch_that_changes_nothing_says_so(wired: Any) -> None:
    """Every field is optional, so `{}` validates and does no work. Answering
    200 would report success for a request that changed no rows."""
    client, db = wired
    db.seed("job_applications", application(id="a"))

    res = client.patch("/tracker/a", json={}, headers=token())
    assert res.status_code == 400
    assert res.json()["detail"]["error"] == "nothing_to_change"


def test_an_unknown_stage_on_a_move_is_refused(wired: Any) -> None:
    """On ADD an unknown stage normalises to a bookmark; on a MOVE it is a
    mistake, because the caller named a stage that does not exist."""
    client, db = wired
    db.seed("job_applications", application(id="a"))

    res = client.patch("/tracker/a", json={"stage": "screening"}, headers=token())
    assert res.status_code == 422
    assert res.json()["detail"]["error"] == "unknown_stage"


# ── Notes and removal ──────────────────────────────────────────────────────


def test_a_logged_update_lands_as_a_note_event(wired: Any) -> None:
    client, db = wired
    db.seed("job_applications", application(id="a"))

    res = client.post("/tracker/a/note", json={"note": "Heard back"}, headers=token())

    assert res.status_code == 201
    events = [row for table, row in db.inserted if table == "application_events"]
    # The fake stamps an `id` the way Postgres would; everything else is ours.
    assert {k: v for k, v in events[0].items() if k != "id"} == {
        "user_id": USER_ID,
        "application_id": "a",
        "event_type": "note",
        "note": "Heard back",
    }


def test_removing_takes_the_timeline_with_it(wired: Any) -> None:
    """An orphan timeline is not a record of anything."""
    client, db = wired
    db.seed("job_applications", application(id="a"))
    db.seed(
        "application_events",
        {"id": "e1", "user_id": USER_ID, "application_id": "a", "event_type": "note", "note": "x"},
    )

    assert client.delete("/tracker/a", headers=token()).status_code == 200
    assert db.tables["job_applications"] == {}
    assert db.tables["application_events"] == {}


def test_a_failed_event_write_never_fails_the_move_that_worked(wired: Any) -> None:
    """The move is done. Reporting failure now would tell the user their action
    failed when it did not, and they would do it twice."""
    client, db = wired
    db.seed("job_applications", application(id="a", status="applied"))
    db.failing.add("application_events")

    res = client.patch("/tracker/a", json={"stage": "offer"}, headers=token())

    assert res.status_code == 200
    assert res.json()["stage"] == "offer"


def test_a_populated_timeline_reads_back_in_full(wired: Any) -> None:
    """Also RECORDS the populated shape.

    `contract_nullable.json` can only describe a state some test produced, and
    the two tests above produce `[]` and `null` — so the event fields the web
    renders would have been in no manifest at all, which is how a surface comes
    to declare a field the API never sends.
    """
    client, db = wired
    db.seed("job_applications", application(id="a", status="interview"))
    db.seed(
        "application_events",
        {
            "id": "e1",
            "user_id": USER_ID,
            "application_id": "a",
            "event_type": "stage_interview",
            "note": "Interview",
            "created_at": (NOW - timedelta(days=1)).isoformat(),
        },
    )
    db.seed(
        "application_events",
        {
            "id": "e2",
            "user_id": USER_ID,
            "application_id": "a",
            "event_type": "note",
            "note": "Heard back",
            "created_at": NOW.isoformat(),
        },
    )

    body = client.get("/tracker/a", headers=token()).json()

    assert {e["event_type"] for e in body["events"]} == {"stage_interview", "note"}
    assert all({"event_type", "note", "created_at"} <= set(e) for e in body["events"])


# ── Closing well, and the column that would grow forever ───────────────────


def test_the_search_can_end_because_you_took_the_offer(wired: Any) -> None:
    """V1's four reasons were Rejected, Withdrew, Ghosted, Accepted elsewhere —
    every one a way for the search to end badly or somewhere else. There was no
    way to record the best outcome the product has."""
    client, db = wired
    db.seed("job_applications", application(id="a", status="offer"))

    body = client.patch(
        "/tracker/a",
        json={"stage": "closed", "closed_reason": "Accepted this offer"},
        headers=token(),
    ).json()

    assert body["closed_reason"] == "Accepted this offer"
    assert "Accepted this offer" in client.get("/tracker", headers=token()).json()["closed_reasons"]


def test_closed_applications_fold_away_but_are_never_removed(wired: Any) -> None:
    """A year's search closes dozens, and the column grows without bound.

    Folding is a view decision; DELETING would trade a permanent loss against a
    presentation problem, and "did I apply here before?" is a question only the
    row can answer.
    """
    client, db = wired
    old = (NOW - timedelta(days=45)).isoformat()
    db.seed("job_applications", application(id="old", status="closed", updated_at=old))
    db.seed("job_applications", application(id="recent", status="closed"))

    closed = next(
        c
        for c in client.get("/tracker", headers=token()).json()["columns"]
        if c["stage"] == "closed"
    )

    assert [i["id"] for i in closed["items"]] == ["recent"]
    assert [i["id"] for i in closed["folded"]] == ["old"]
    # The count still counts them. The column knows what it is holding.
    assert closed["count"] == 2


def test_a_closed_row_with_no_readable_date_stays_visible(wired: Any) -> None:
    """Hiding a row on the strength of a timestamp we could not read is the
    falsy-zero mistake wearing a different hat."""
    client, db = wired
    db.seed(
        "job_applications",
        application(id="a", status="closed", updated_at=None, created_at=None),
    )

    closed = next(
        c
        for c in client.get("/tracker", headers=token()).json()["columns"]
        if c["stage"] == "closed"
    )
    assert [i["id"] for i in closed["items"]] == ["a"]
    assert closed["folded"] == []


def test_only_closed_folds(wired: Any) -> None:
    """An application sitting in `applied` for 112 days is the opposite of
    something to hide — five live rows are in exactly that state."""
    client, db = wired
    db.seed(
        "job_applications",
        application(id="a", status="applied", updated_at=(NOW - timedelta(days=112)).isoformat()),
    )

    applied = next(
        c
        for c in client.get("/tracker", headers=token()).json()["columns"]
        if c["stage"] == "applied"
    )
    assert len(applied["items"]) == 1
    assert applied["folded"] == []


def test_following_up_resets_the_clock_and_says_so_on_the_timeline(wired: Any) -> None:
    """The nudge only cleared when a STAGE moved, so an application you did
    chase went on asking to be chased."""
    client, db = wired
    db.seed(
        "job_applications",
        application(id="a", status="applied", updated_at=(NOW - timedelta(days=30)).isoformat()),
    )
    assert client.get("/tracker/a", headers=token()).json()["needs_follow_up"] is True

    body = client.post("/tracker/a/followed-up", headers=token()).json()

    assert body["needs_follow_up"] is False
    assert body["days_in_stage"] == 0
    events = [row for table, row in db.inserted if table == "application_events"]
    assert events[-1]["note"] == "Followed up"
