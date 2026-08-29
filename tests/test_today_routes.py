"""Today and the thread, over HTTP.

Fixtures reflect what the tables ACTUALLY hold: `tracker`, `content_loop` and
`thread_decisions` are empty, `score_history` covers 10 of 12 users and
`jobs_feed` 6 of 12. The common beta state is partial, so that is the default
case here rather than the exception.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from carigma_api.routes import today as routes
from carigma_api.services.ladder import Signals
from carigma_api.services.thread_store import SupabaseThreadStore
from tests.conftest import auth, make_token

NOW = datetime.now(UTC)


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

    def order(self, *_a: Any, **_k: Any) -> FakeTable:
        return self

    def execute(self) -> Any:
        table = self._db.tables.setdefault(self._name, {})
        if self._name in self._db.failing:
            raise RuntimeError(f"{self._name} is down")
        if self._op == "upsert":
            assert self._payload is not None and self._conflict
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
        self.failing: set[str] = set()

    def table(self, name: str) -> FakeTable:
        return FakeTable(self, name)

    def seed(self, name: str, key: tuple[Any, ...], row: dict[str, Any]) -> None:
        self.tables.setdefault(name, {})[key] = row


class FakeProfiles:
    def __init__(self, profile: dict[str, Any] | None = None) -> None:
        self.profile = {"name": "Ravi Kumar", "onboarded": True} if profile is None else profile
        self.fail = False

    def load(self, user_id: str) -> dict[str, Any]:
        if self.fail:
            raise RuntimeError("profiles down")
        return dict(self.profile)


@pytest.fixture
def wired(client: TestClient, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    db, profiles = FakeDB(), FakeProfiles()
    thread = SupabaseThreadStore(db)
    monkeypatch.setattr(routes, "_deps", lambda request, settings: (db, profiles, thread))
    return client, db, profiles


def token() -> dict[str, str]:
    return auth(make_token())


# ── Auth ───────────────────────────────────────────────────────────────────


def test_every_endpoint_requires_a_verified_token(client: TestClient) -> None:
    assert client.get("/today").status_code == 401
    assert client.get("/thread").status_code == 401
    assert client.post("/thread/post_day/decide", json={"decision": "dismissed"}).status_code == 401


# ── The partial state most beta users are in ───────────────────────────────


def test_an_empty_tracker_and_no_week_still_renders(wired) -> None:  # type: ignore[no-untyped-def]
    """`tracker` and `content_loop` are empty for every live user. Today must
    work in that state rather than assuming a fully populated one."""
    client, _db, _profiles = wired

    res = client.get("/today", headers=token())

    assert res.status_code == 200
    assert res.json()["primary_action"]["key"] == "standing"


def test_nothing_pressing_does_not_invent_work(wired) -> None:  # type: ignore[no-untyped-def]
    client, _db, _profiles = wired

    body = client.get("/today", headers=token()).json()

    assert "Nothing is due" in body["primary_action"]["title"]
    assert body["deferred"] == []

    # The route must still hand back somewhere to go. "Does not invent work"
    # and "offers nothing" are different answers, and the old copy drifted
    # toward the second while the button said otherwise.
    assert body["primary_action"]["primary"]["route"]
    assert "come back" not in body["primary_action"]["body"].lower()


def test_the_greeting_uses_a_first_name_when_there_is_one(wired) -> None:  # type: ignore[no-untyped-def]
    client, _db, _profiles = wired

    assert "Ravi" in client.get("/today", headers=token()).json()["greeting"]


def test_a_profile_with_no_name_is_greeted_without_one(wired) -> None:  # type: ignore[no-untyped-def]
    """Two live accounts have no name. "Morning, undefined" is the shape of
    bug this codebase keeps deleting."""
    client, _db, profiles = wired
    profiles.profile = {"onboarded": False}

    greeting = client.get("/today", headers=token()).json()["greeting"]

    assert greeting in ("Morning", "Afternoon", "Evening")
    assert "None" not in greeting


# ── The last-seen distinction ──────────────────────────────────────────────


def test_no_last_seen_never_says_everything_is_new(wired) -> None:  # type: ignore[no-untyped-def]
    """Four of twelve profiles have no `lastSeenJobsAt`. This is the first
    thing such a user would ever read on Today."""
    client, db, profiles = wired
    profiles.profile = {"name": "Ravi", "onboarded": True}
    for i in range(15):
        db.seed(
            "jobs_feed",
            (i,),
            {
                "user_id": "11111111-1111-1111-1111-111111111111",
                "status": "active",
                "first_seen": NOW.isoformat(),
            },
        )

    body = client.get("/today", headers=token()).json()

    assert body["primary_action"]["title"] == "15 matches waiting"
    assert "new" not in body["primary_action"]["title"]


def test_a_known_last_seen_counts_only_what_arrived_since(wired) -> None:  # type: ignore[no-untyped-def]
    client, db, profiles = wired
    profiles.profile = {
        "name": "Ravi",
        "onboarded": True,
        "lastSeenJobsAt": (NOW - timedelta(days=1)).isoformat(),
    }
    uid = "11111111-1111-1111-1111-111111111111"
    db.seed(
        "jobs_feed",
        (1,),
        {"user_id": uid, "status": "active", "first_seen": (NOW - timedelta(days=3)).isoformat()},
    )
    db.seed("jobs_feed", (2,), {"user_id": uid, "status": "active", "first_seen": NOW.isoformat()})

    body = client.get("/today", headers=token()).json()

    assert body["primary_action"]["title"] == "1 new match"


def test_reading_today_does_not_stamp_the_feed_as_seen(wired) -> None:  # type: ignore[no-untyped-def]
    """Marking the feed seen because Today rendered a count would make the
    number right exactly once, and would consume the "new" state of jobs the
    user has not opened. The Jobs surface owns that stamp."""
    client, db, profiles = wired
    profiles.profile = {"name": "Ravi", "lastSeenJobsAt": (NOW - timedelta(days=1)).isoformat()}
    db.seed(
        "jobs_feed",
        (1,),
        {
            "user_id": "11111111-1111-1111-1111-111111111111",
            "status": "active",
            "first_seen": NOW.isoformat(),
        },
    )

    first = client.get("/today", headers=token()).json()["primary_action"]["title"]
    second = client.get("/today", headers=token()).json()["primary_action"]["title"]

    assert first == second == "1 new match"


# ── Failure degrades to silence, never to a fabricated signal ──────────────


def test_a_broken_tracker_does_not_take_the_whole_screen_down(wired) -> None:  # type: ignore[no-untyped-def]
    """Losing four working rungs to one broken source would be a worse answer
    than losing the rung."""
    client, db, profiles = wired
    db.failing.add("tracker")
    profiles.profile = {"name": "Ravi", "lastSeenJobsAt": (NOW - timedelta(days=1)).isoformat()}
    db.seed(
        "jobs_feed",
        (1,),
        {
            "user_id": "11111111-1111-1111-1111-111111111111",
            "status": "active",
            "first_seen": NOW.isoformat(),
        },
    )

    res = client.get("/today", headers=token())

    assert res.status_code == 200
    assert res.json()["primary_action"]["key"] == "new_matches"


def test_an_unreadable_profile_is_a_503_not_a_guess(wired) -> None:  # type: ignore[no-untyped-def]
    """Without the profile there is no `lastSeenJobsAt`, and guessing produces
    exactly the "everything is new" claim this path exists to avoid."""
    client, _db, profiles = wired
    profiles.fail = True

    res = client.get("/today", headers=token())

    assert res.status_code == 503
    assert res.json()["detail"]["error"] == "read_failed"


def test_unreadable_decisions_show_everything_rather_than_hiding_it(wired) -> None:  # type: ignore[no-untyped-def]
    """Not knowing what was dismissed must not silently hide items the user
    never decided about."""
    client, db, profiles = wired
    db.failing.add("thread_decisions")
    profiles.profile = {"name": "Ravi", "lastSeenJobsAt": (NOW - timedelta(days=1)).isoformat()}
    db.seed(
        "jobs_feed",
        (1,),
        {
            "user_id": "11111111-1111-1111-1111-111111111111",
            "status": "active",
            "first_seen": NOW.isoformat(),
        },
    )

    res = client.get("/today", headers=token())

    assert res.status_code == 200
    assert res.json()["primary_action"]["key"] == "new_matches"


# ── The decision round trip ────────────────────────────────────────────────


def test_a_dismissal_survives_into_the_next_request(wired) -> None:  # type: ignore[no-untyped-def]
    """THE test for this surface. A rung that reappears after being dismissed
    is worse than one that was never dismissible."""
    client, db, profiles = wired
    profiles.profile = {"name": "Ravi", "lastSeenJobsAt": (NOW - timedelta(days=1)).isoformat()}
    uid = "11111111-1111-1111-1111-111111111111"
    db.seed("jobs_feed", (1,), {"user_id": uid, "status": "active", "first_seen": NOW.isoformat()})
    db.seed("content_loop", (1,), {})  # nothing; the fix rung is what we dismiss

    before = client.get("/thread", headers=token()).json()["items"]
    assert "new_matches" in [i["key"] for i in before]

    client.post("/thread/new_matches/decide", json={"decision": "dismissed"}, headers=token())
    after = client.get("/thread", headers=token()).json()["items"]

    assert "new_matches" not in [i["key"] for i in after]


def test_the_decision_response_is_read_back_from_the_row(wired) -> None:  # type: ignore[no-untyped-def]
    """Echoing the request would confirm a decision that may not have landed."""
    client, _db, _profiles = wired

    body = client.post(
        "/thread/post_day/decide", json={"decision": "snoozed"}, headers=token()
    ).json()

    assert body["decision"] == "snoozed"
    assert body["snooze_until"], "a snooze with no deadline is a state nothing can leave"


def test_a_failed_save_says_nothing_changed(wired) -> None:  # type: ignore[no-untyped-def]
    client, db, _profiles = wired
    db.failing.add("thread_decisions")

    res = client.post("/thread/post_day/decide", json={"decision": "dismissed"}, headers=token())

    assert res.status_code == 503
    assert "Nothing has changed" in res.json()["detail"]["message"]


def test_an_unknown_decision_is_refused(wired) -> None:  # type: ignore[no-untyped-def]
    client, _db, _profiles = wired

    res = client.post("/thread/post_day/decide", json={"decision": "deleted"}, headers=token())

    assert res.status_code == 422


# ── Today and the thread agree ─────────────────────────────────────────────


def test_the_thread_is_the_whole_list_and_today_is_its_head(wired) -> None:  # type: ignore[no-untyped-def]
    """Not a second opinion. Two computations of "what is going on" would
    eventually disagree on one screen."""
    client, db, profiles = wired
    profiles.profile = {"name": "Ravi", "lastSeenJobsAt": (NOW - timedelta(days=1)).isoformat()}
    db.seed(
        "jobs_feed",
        (1,),
        {
            "user_id": "11111111-1111-1111-1111-111111111111",
            "status": "active",
            "first_seen": NOW.isoformat(),
        },
    )

    today = client.get("/today", headers=token()).json()
    thread = client.get("/thread", headers=token()).json()

    assert thread["items"][0]["key"] == today["primary_action"]["key"]


def test_nothing_on_this_path_charges_credits() -> None:
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(routes))
    referenced: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            referenced.add(node.id)
        elif isinstance(node, ast.Attribute):
            referenced.add(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            referenced.add(node.value)

    for forbidden in ("credits", "credit_ledger", "apply_delta", "charge_for_result"):
        assert forbidden not in referenced, f"{forbidden!r} appears on a free path"


def test_the_signals_type_carries_no_extra_state() -> None:
    """The ladder is a pure function of stated facts. A field added here
    without a rung reading it is a signal nothing acts on."""
    fields = set(Signals.__dataclass_fields__)

    assert fields == {
        "now",
        "next_interview_at",
        "interview_label",
        "post_due_today",
        "pending_fix",
        "new_match_count",
        "open_match_count",
    }


def test_an_upcoming_interview_reaches_the_screen_with_its_time(wired) -> None:  # type: ignore[no-untyped-def]
    """The interview rung end to end, through HTTP.

    Added because the web's payload guard reported `when` as a field the API
    never sends — true, and only because no test had ever produced an
    interview primary action. The shape was unverified rather than absent,
    which is the UNVERIFIED category the audit found two of.
    """
    client, db, profiles = wired
    uid = "11111111-1111-1111-1111-111111111111"
    soon = NOW + timedelta(hours=30)
    db.seed(
        "tracker",
        (1,),
        {
            "user_id": uid,
            "stage": "interview",
            "company": "Zomato",
            "title": "Data Analyst",
            "interview_at": soon.isoformat(),
            "closed_at": None,
        },
    )

    body = client.get("/today", headers=token()).json()
    primary = body["primary_action"]

    assert primary["key"] == "interview_soon"
    assert primary["time_bound"] is True
    assert primary["when"], "an interview rung with no time is not time-bound"
    assert "Zomato" in primary["body"]

    # And through /thread, which carries the same rung at a different path.
    # The web checks LadderItem against BOTH `primary_action` and `items[]`,
    # so a shape verified on one and not the other is still half unverified.
    head = client.get("/thread", headers=token()).json()["items"][0]
    assert head["key"] == "interview_soon"
    assert head["when"] == primary["when"]


def test_a_closed_application_is_not_an_upcoming_interview(wired) -> None:  # type: ignore[no-untyped-def]
    """An interview for a role that is gone is not something to prepare for."""
    client, db, _profiles = wired
    uid = "11111111-1111-1111-1111-111111111111"
    db.seed(
        "tracker",
        (1,),
        {
            "user_id": uid,
            "stage": "closed",
            "company": "Zomato",
            "title": "Data Analyst",
            "interview_at": (NOW + timedelta(hours=5)).isoformat(),
            "closed_at": NOW.isoformat(),
        },
    )

    assert client.get("/today", headers=token()).json()["primary_action"]["key"] == "standing"
