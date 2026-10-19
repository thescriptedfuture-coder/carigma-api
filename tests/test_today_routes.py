"""Today and the thread, over HTTP.

Fixtures reflect what the tables ACTUALLY hold: `job_applications` carries
thirteen rows for one user, `content_loop` and
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
from tests.conftest import USER_ID, auth, make_token

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

    def limit(self, _n: int) -> FakeTable:
        # The score history read is ordered and limited. Without this the chain
        # raised AttributeError, the read was swallowed as "unreadable", and
        # the recorded contract carried `signal.score: null` for every test —
        # a shape the real API only produces when the table is DOWN.
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
    db.failing.add("job_applications")
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
        "interview_id",
        "interview_label",
        "post_due_today",
        "pending_fix",
        "new_match_count",
        "open_match_count",
        # S9: the two weekly rungs.
        "unsigned_week",
        "unsigned_posts",
        "review_label",
        "review_lines",
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
        "job_applications",
        (1,),
        {
            "user_id": uid,
            "status": "interview",
            "company": "Zomato",
            "role": "Data Analyst",
            # A DATE, as the live column holds. Parsed naively it compares to
            # an aware `now` with TypeError and takes the whole screen down.
            "interview_on": soon.date().isoformat(),
            "closed_reason": None,
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
        "job_applications",
        (1,),
        {
            "user_id": uid,
            "status": "closed",
            "company": "Zomato",
            "role": "Data Analyst",
            "interview_on": (NOW + timedelta(hours=5)).date().isoformat(),
            "closed_reason": "Ghosted",
        },
    )

    assert client.get("/today", headers=token()).json()["primary_action"]["key"] == "standing"


# ── The signal row and the week strip (spec 3.2) ───────────────────────────


def test_today_carries_the_numbers_as_well_as_the_action(wired) -> None:  # type: ignore[no-untyped-def]
    """Where "WHAT CARIGMA DOES" used to be."""
    client, db, profiles = wired
    uid = "11111111-1111-1111-1111-111111111111"
    profiles.profile = {"name": "Ravi", "lastSeenJobsAt": (NOW - timedelta(days=2)).isoformat()}
    db.seed("job_applications", (1,), {"user_id": uid, "status": "applied"})
    db.seed("job_applications", (2,), {"user_id": uid, "status": "interview"})
    db.seed(
        "jobs_feed",
        (1,),
        {
            "user_id": uid,
            "id": 4412,
            "title": "Data Analyst",
            "company": "Zomato",
            "match_score": 82,
            "status": "active",
            "first_seen": NOW.isoformat(),
        },
    )
    db.seed(
        "score_history",
        (1,),
        {"user_id": uid, "score": 72, "created_at": NOW.isoformat()},
    )
    db.seed(
        "score_history",
        (2,),
        {"user_id": uid, "score": 66, "created_at": (NOW - timedelta(days=14)).isoformat()},
    )

    body = client.get("/today", headers=token()).json()

    assert body["signal"]["score"] == {
        "value": 72,
        "delta_30d": 6,
        "measured_at": NOW.isoformat(),
    }
    assert body["signal"]["pipeline"] == {"active": 2, "interviewing": 1}
    assert body["signal"]["matches"] == {
        "new": 1,
        "open": 1,
        # Named, not only counted. "2 new matches" is a number; "Data Analyst
        # at Zomato, 82" is a reason to open the page.
        "top": [{"id": 4412, "title": "Data Analyst", "company": "Zomato", "match_score": 82}],
    }
    assert [cell["day"] for cell in body["week"]] == [
        "MON",
        "TUE",
        "WED",
        "THU",
        "FRI",
        "SAT",
        "SUN",
    ]
    assert sum(1 for cell in body["week"] if cell["is_today"]) == 1


def test_a_broken_tracker_reports_no_pipeline_rather_than_an_empty_one(wired) -> None:  # type: ignore[no-untyped-def]
    """The whole reason `Sources` keeps `None` apart from `[]`.

    "0 active · 0 interviewing" is a statement about someone's job search. The
    table being down is a statement about us.
    """
    client, db, profiles = wired
    db.failing.add("job_applications")
    profiles.profile = {"name": "Ravi"}

    body = client.get("/today", headers=token()).json()

    assert body["signal"]["pipeline"] is None
    # And the rest of the row still answers.
    assert body["signal"]["matches"] == {"new": None, "open": 0, "top": []}


def test_a_broken_score_history_reports_no_score_rather_than_zero(wired) -> None:  # type: ignore[no-untyped-def]
    client, db, profiles = wired
    db.failing.add("score_history")
    profiles.profile = {"name": "Ravi"}

    assert client.get("/today", headers=token()).json()["signal"]["score"] is None


def test_a_broken_feed_and_a_broken_plan_report_nothing_rather_than_empty(wired) -> None:  # type: ignore[no-untyped-def]
    """Records the `null` shapes as well as asserting them.

    `contract_nullable.json` can only say a key is nullable if some test has
    actually produced it null — a key list cannot express "can appear empty",
    which is why the web's fixtures had ninety-six states nothing covered.
    """
    client, db, profiles = wired
    db.failing.update({"jobs_feed", "content_loop", "content_weeks"})
    profiles.profile = {"name": "Ravi"}

    body = client.get("/today", headers=token()).json()

    assert body["signal"]["matches"] is None
    assert body["week"] is None


def test_the_interview_card_names_the_role_and_opens_that_application(wired) -> None:  # type: ignore[no-untyped-def]
    """It said "Prep while there is still time to use it" behind a button
    labelled "Open prep", and Interview Intelligence is not built.

    The most time-bound card the product has was promising the one thing it
    cannot do, and pointing at the board rather than the application.
    """
    client, db, profiles = wired
    uid = "11111111-1111-1111-1111-111111111111"
    profiles.profile = {"name": "Ravi"}
    db.seed(
        "job_applications",
        (1,),
        {
            "user_id": uid,
            "id": "app-7",
            "status": "interview",
            "company": "CRED",
            "role": "Data Analyst",
            "interview_on": (NOW + timedelta(days=2)).date().isoformat(),
        },
    )

    card = client.get("/today", headers=token()).json()["primary_action"]

    assert card["key"] == "interview_soon"
    assert "CRED" in card["body"]
    assert "prep" not in card["body"].lower()
    assert card["primary"] == {"label": "Open the application", "route": "/tracker/app-7"}


def test_the_week_strip_follows_the_week_the_user_signed(wired) -> None:  # type: ignore[no-untyped-def]
    """S8. The strip read the stored slots, and Posts seeded Monday and
    Thursday for everyone — so a week signed for Thursday alone showed two
    posting days on Today."""
    from datetime import timedelta

    from carigma_api.services.posts import Slot, mark_published
    from carigma_api.services.posts_store import slot_to_row
    from carigma_api.services.weekly import ContractItem, ContractState, WeeklyContract
    from carigma_api.services.weekly_store import to_row

    client, db, profiles = wired
    profiles.profile = {"name": "Ravi"}
    today = datetime.now(UTC).date()
    monday = today - timedelta(days=today.weekday())
    signed = WeeklyContract(
        week_start=monday,
        state=ContractState.APPROVED,
        items=(ContractItem("content", "THU", "One post slot", 10, 3),),
        accepted_kinds=("content",),
    )
    db.seed("weekly_review", (USER_ID, monday.isoformat()), to_row(USER_ID, signed))
    shipped = mark_published(Slot("MON", monday), at=f"{monday.isoformat()}T16:36:00+00:00")
    db.seed(
        "content_loop", (USER_ID, monday.isoformat(), "MON"), slot_to_row(USER_ID, monday, shipped)
    )

    week = {cell["day"]: cell for cell in client.get("/today", headers=token()).json()["week"]}

    assert week["THU"]["state"] == "pending"
    assert week["THU"]["optional"] is False
    # The post he shipped outside the plan is on the strip, marked as such.
    assert (week["MON"]["state"], week["MON"]["optional"]) == ("published", True)
    assert {day for day, cell in week.items() if cell["state"] != "none"} == {"MON", "THU"}


# ── The reader's clock, and the Sunday rungs (S9) ──────────────────────────


def _at(monkeypatch: pytest.MonkeyPatch, moment: datetime) -> None:
    class _Clock(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:  # type: ignore[override]
            return moment

    monkeypatch.setattr(routes, "datetime", _Clock)


@pytest.mark.parametrize(
    ("utc", "word"),
    [
        # 9:05 pm in India. It said "Afternoon": the server's UTC hour.
        (datetime(2026, 10, 19, 15, 35, tzinfo=UTC), "Evening"),
        (datetime(2026, 10, 19, 3, 0, tzinfo=UTC), "Morning"),  # 08:30 IST
        (datetime(2026, 10, 19, 7, 0, tzinfo=UTC), "Afternoon"),  # 12:30 IST
        (datetime(2026, 10, 19, 21, 0, tzinfo=UTC), "Evening"),  # 02:30 IST
    ],
)
def test_the_greeting_is_said_on_the_readers_clock(
    wired, monkeypatch: pytest.MonkeyPatch, utc: datetime, word: str
) -> None:  # type: ignore[no-untyped-def]
    client, _db, profiles = wired
    profiles.profile = {"name": "Ravi Kumar"}
    _at(monkeypatch, utc)

    assert client.get("/today", headers=token()).json()["greeting"] == f"{word}, Ravi"


def _seed_week(db, monday, state, *, days=("MON", "TUE", "WED", "THU", "FRI"), facts=None):  # type: ignore[no-untyped-def]
    from carigma_api.services.weekly import ContractItem, ContractState, WeeklyContract
    from carigma_api.services.weekly_store import to_row

    contract = WeeklyContract(
        week_start=monday,
        state=ContractState(state),
        items=tuple(ContractItem("content", d, "One post slot", 10, 3) for d in days),
        accepted_kinds=("content",) if state == "approved" else (),
    )
    row = to_row(USER_ID, contract)
    if facts is not None:
        row["facts"] = {"facts": facts, "streak": {"weeks": 1}, "built_at": "x"}
    db.seed("weekly_review", (USER_ID, monday.isoformat()), row)


def _fact(text: str) -> dict[str, str]:
    return {"direction": "done", "glyph": "✓", "text": text, "receipt": "EVIDENCE"}


def test_on_sunday_evening_today_asks_for_next_weeks_signature(
    wired, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    from datetime import date

    client, db, profiles = wired
    profiles.profile = {"name": "Ravi"}
    _at(monkeypatch, datetime(2026, 10, 25, 13, 0, tzinfo=UTC))  # Sunday, 18:30 IST
    _seed_week(db, date(2026, 10, 19), "approved", facts=[_fact("a"), _fact("b")])
    _seed_week(db, date(2026, 10, 26), "proposed")

    primary = client.get("/today", headers=token()).json()["primary_action"]

    assert primary["key"] == "sign_week"
    assert primary["title"] == "Next week's plan is ready to sign"
    assert primary["body"].startswith("Your review of 19–25 Oct is in — 2 lines.")
    assert primary["primary"]["route"] == "/progress"


def test_on_monday_the_review_is_on_today_beside_the_days_post(
    wired, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    """Ravi looked for the review on Today and it was not there: it lived
    only on Progress."""
    from datetime import date

    client, db, profiles = wired
    profiles.profile = {"name": "Ravi"}
    _at(monkeypatch, datetime(2026, 10, 19, 15, 35, tzinfo=UTC))
    _seed_week(db, date(2026, 10, 12), "approved", days=("THU",), facts=[_fact("a")])
    _seed_week(db, date(2026, 10, 19), "approved")

    body = client.get("/today", headers=token()).json()
    review = next(d for d in body["deferred"] if d["key"] == "week_review")

    assert review["body"] == "12–18 Oct · 1 line, each with its evidence."
    assert review["primary"] == {"label": "Read it", "route": "/progress"}


def test_an_unsigned_week_is_never_reported_as_a_quiet_day(
    wired, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    from datetime import date

    client, db, profiles = wired
    profiles.profile = {"name": "Ravi"}
    _at(monkeypatch, datetime(2026, 10, 21, 6, 0, tzinfo=UTC))  # Wednesday
    _seed_week(db, date(2026, 10, 19), "proposed")

    primary = client.get("/today", headers=token()).json()["primary_action"]

    assert primary["title"] == "This week's plan is waiting for your signature"


def test_unreadable_weeks_ask_for_no_signature(wired, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    """A card asking someone to sign a week they may have signed is the claim
    we could not check. Absence, and the rest of Today still renders."""
    client, db, profiles = wired
    profiles.profile = {"name": "Ravi"}
    db.failing.add("weekly_review")

    res = client.get("/today", headers=token())

    assert res.status_code == 200
    keys = {res.json()["primary_action"]["key"], *(d["key"] for d in res.json()["deferred"])}
    assert not keys & {"sign_week", "week_review"}
