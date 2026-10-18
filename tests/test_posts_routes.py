"""Posts over HTTP.

The endpoint surface is where the "we never auto-post" rule becomes checkable
by someone who never reads the service layer: there is no publish endpoint, and
the only thing that advances the streak is the user saying they shipped it.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from carigma_api.routes import posts as posts_routes
from carigma_api.services.posts import (
    Draft,
    Slot,
    SlotState,
    WeekPlan,
    default_week,
    mark_published,
)
from carigma_api.services.posts_store import slot_from_row, slot_to_row, week_to_row
from tests.conftest import USER_ID, auth, make_token


class FakePlanStore:
    """An in-memory store that goes through the REAL row mapping.

    Not a dict of `WeekPlan` objects. `slot_to_row` / `slot_from_row` run on
    every write and read, so a mapping that drops a field fails here rather
    than in production — the fake-simpler-than-reality trap that hid the
    profile-retention bug.

    Keyed on `(user_id, week_start, day)`, the real unique constraint, so a
    duplicate day is impossible here exactly as it is impossible there.
    """

    def __init__(self) -> None:
        self.slot_rows: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.week_rows: dict[tuple[str, str], dict[str, Any]] = {}
        self.slot_writes = 0

    def load(self, user_id: str, week_start: date) -> WeekPlan | None:
        rows = [
            r
            for (u, w, _d), r in self.slot_rows.items()
            if u == user_id and w == week_start.isoformat()
        ]
        week = self.week_rows.get((user_id, week_start.isoformat()), {})
        # Existence is the WEEK row, not the slots — matching the real store.
        # A paused week has zero slots legitimately, and a fake that returned
        # None here would disagree with production about what absence means.
        if not rows and not week:
            return None
        base = default_week(week_start)
        slots = tuple(sorted((slot_from_row(r) for r in rows), key=lambda s: s.slot_date))
        return WeekPlan(
            week_start=week_start,
            slots=slots,
            cadence_per_week=(
                int(raw)
                if (raw := week.get("cadence_per_week")) is not None
                else base.cadence_per_week
            ),
            cadence_days=tuple(week.get("cadence_days") or base.cadence_days),
            streak_weeks=int(week.get("streak_weeks") or 0),
            paused_until=(
                date.fromisoformat(str(week["paused_until"])) if week.get("paused_until") else None
            ),
        )

    def save_slot(self, user_id: str, week_start: date, slot: Slot) -> None:
        self.slot_writes += 1
        row = slot_to_row(user_id, week_start, slot)
        # `published_at` is `timestamptz`. Postgres refuses anything that is
        # not a moment — and this fake accepted "13:05" for as long as the
        # route sent it, which is how every "Mark published" in V2 failed in
        # production with every test here green. A fake that cannot refuse
        # what the database refuses tests a database that does not exist.
        if row["published_at"] is not None:
            try:
                datetime.fromisoformat(str(row["published_at"]))
            except ValueError as exc:
                raise RuntimeError(
                    "invalid input syntax for type timestamp with time zone: "
                    f"{row['published_at']!r} (22007)"
                ) from exc
        self.slot_rows[(user_id, week_start.isoformat(), slot.day)] = row

    def save_week(self, user_id: str, plan: WeekPlan) -> None:
        self.week_rows[(user_id, plan.week_start.isoformat())] = week_to_row(user_id, plan)
        for slot in plan.slots:
            self.save_slot(user_id, plan.week_start, slot)


STORE = FakePlanStore()


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    global STORE
    STORE = FakePlanStore()
    monkeypatch.setattr(posts_routes, "_store", lambda request, settings: STORE)
    yield


def monday() -> date:
    today = datetime.now(UTC).date()
    return today - timedelta(days=today.weekday())


def seed_week(**kw: object) -> WeekPlan:
    base = default_week(monday())
    plan = WeekPlan(
        week_start=base.week_start,
        slots=base.slots,
        cadence_days=base.cadence_days,
        cadence_per_week=base.cadence_per_week,
        **kw,  # type: ignore[arg-type]
    )
    STORE.save_week(USER_ID, plan)
    return plan


def seed_with(*slots: Slot, streak: int = 0) -> None:
    STORE.save_week(USER_ID, WeekPlan(week_start=monday(), slots=slots, streak_weeks=streak))


# ── There is no publish endpoint ───────────────────────────────────────────


def test_the_api_exposes_no_endpoint_that_posts_on_the_users_behalf(
    client: TestClient,
) -> None:
    """The strongest form of "we never auto-post": the capability is absent.

    A future contributor cannot accidentally call something that doesn't exist.
    """
    # Read the OpenAPI schema rather than walking app.routes: included routers
    # are nested wrappers without a .path, so the naive walk returned an empty
    # list and made this test vacuously pass. The precondition below caught it.
    paths = list(client.app.openapi()["paths"])  # type: ignore[attr-defined]

    # Precondition: the posts router must actually be mounted, or "no publish
    # endpoint exists" would be trivially true of an unmounted API.
    posts_paths = [p for p in paths if p.startswith("/posts")]
    assert posts_paths, "the posts router is not mounted — this test proves nothing"

    posting = [p for p in posts_paths if p.rstrip("/").endswith(("/publish", "/share", "/post"))]
    assert posting == [], f"an endpoint that could post for the user: {posting}"
    assert "/posts/week/{day}/mark-published" in paths


# ── Reading ────────────────────────────────────────────────────────────────


def test_the_week_needs_a_verified_token(client: TestClient) -> None:
    assert client.get("/posts/week").status_code == 401


def test_the_week_carries_its_cadence_and_provenance(client: TestClient) -> None:
    seed_week()
    body = client.get("/posts/week", headers=auth(make_token())).json()

    assert body["cadence_label"] == "2/wk · Mon + Thu"
    assert body["provenance"]["agent_label"] == "Content Intelligence"
    # The line must describe something that exists. Nothing writes drafts on a
    # schedule, so the old "drafts land 07:00 on slot days" was a claim about a
    # worker nobody built, shown on the surface where the box is then empty.
    assert body["provenance"]["line"] == "drafts are written when you ask, 3 credits a slot"


def test_the_week_carries_each_slot_s_drafts(client: TestClient) -> None:
    """The week list shows variants, so it carries them — and until this test
    nothing had ever produced a week containing one.

    `GET /posts/week/{day}` had drafts in its recorded contract; `GET
    /posts/week` did not, because every seeded week here was empty. The web's
    own fixture carried draft bodies at the week level, and the guard comparing
    the two read that as the MOCK inventing a field. The API can send it; no
    test made it.
    """
    seed_with(
        Slot(
            "THU",
            monday() + timedelta(days=3),
            state=SlotState.DRAFT_READY,
            drafts=(Draft(1, "Why do dashboards get ignored?", "stronger hook"),),
        )
    )

    body = client.get("/posts/week", headers=auth(make_token())).json()
    slot = next(s for s in body["slots"] if s["day"] == "THU")

    assert slot["variant_count"] == 1
    assert slot["drafts"][0]["body"].startswith("Why do dashboards")
    assert slot["drafts"][0]["note"] == "stronger hook"


def test_a_paused_week_is_an_honest_nothing_not_an_empty_list(
    client: TestClient,
) -> None:
    STORE.save_week(
        USER_ID, WeekPlan(week_start=monday(), slots=(), cadence_per_week=0, streak_weeks=6)
    )
    body = client.get("/posts/week", headers=auth(make_token())).json()

    assert set(body["empty"]) == {"happened", "why", "next"}
    assert "don't accumulate" in body["empty"]["why"]
    # Held, not lost.
    assert body["streak"]["sentence"] == "Paused at 6 weeks"


def test_the_editor_states_that_we_never_post_for_you(client: TestClient) -> None:
    seed_week()
    body = client.get("/posts/week/THU", headers=auth(make_token())).json()

    assert "never posts for you" in body["disclaimer"]
    assert [s["step"] for s in body["flow"]] == [1, 2, 3]
    assert "you say so" in body["flow"][2]["label"]
    # WHERE it goes, said in the step rather than left to guess.
    assert "LinkedIn" in body["flow"][1]["label"]
    assert "never posts for you" in body["flow"][1]["detail"]
    assert body["why"]


def test_the_guidance_counts_the_drafts_that_are_really_there(client: TestClient) -> None:
    """The check drops drafts that invent, so "three drafts" is not a given."""
    seed_with(
        Slot(
            "THU",
            monday() + timedelta(days=3),
            state=SlotState.DRAFT_READY,
            drafts=(Draft(index=1, body="One survived.", note="Story"),),
        )
    )
    body = client.get("/posts/week/THU", headers=auth(make_token())).json()

    assert body["flow"][0]["detail"].startswith("Your draft is ready")


def test_the_editor_says_which_skip_reasons_retrain_the_agent(
    client: TestClient,
) -> None:
    seed_week()
    reasons = client.get("/posts/week/THU", headers=auth(make_token())).json()["skip_reasons"]
    by_value = {r["value"]: r["retrains"] for r in reasons}

    assert by_value["not_my_topic"] is True
    assert by_value["drafts_weak"] is True
    assert by_value["no_time"] is False


def test_a_day_with_no_slot_says_so(client: TestClient) -> None:
    seed_week()
    res = client.get("/posts/week/WED", headers=auth(make_token()))

    assert res.status_code == 404
    assert res.json()["detail"]["error"] == "no_slot"


def test_the_review_panel_reflects_the_actual_draft(client: TestClient) -> None:
    """A fixed checklist would claim to have checked text it never read."""
    seed_with(
        Slot(
            "THU",
            monday() + timedelta(days=3),
            state=SlotState.DRAFT_READY,
            drafts=(Draft(1, "Why do dashboards get ignored?\nBecause nobody asked."),),
        )
    )
    body = client.get("/posts/week/THU", headers=auth(make_token())).json()
    notes = {n["text"]: n["ok"] for n in body["review"]}

    assert notes["Hook — question in line 1"] is True
    assert notes["Ending asks something"] is False


def test_a_slot_with_no_draft_claims_no_checks(client: TestClient) -> None:
    seed_with(Slot("THU", monday() + timedelta(days=3)))
    body = client.get("/posts/week/THU", headers=auth(make_token())).json()
    assert body["review"] == []


# ── Marking published ──────────────────────────────────────────────────────


def test_marking_published_is_free_and_advances_the_streak(client: TestClient) -> None:
    seed_with(
        Slot("MON", monday(), state=SlotState.DRAFT_READY),
        Slot(
            "THU",
            monday() + timedelta(days=3),
            state=SlotState.PUBLISHED,
            published_at="2026-08-06T09:04:00+05:30",
        ),
        streak=6,
    )
    res = client.post(
        "/posts/week/MON/mark-published",
        json={"at": "2026-08-03T09:02:00+05:30"},
        headers=auth(make_token()),
    )

    assert res.status_code == 200
    body = res.json()
    assert body["charged"] == 0, "telling us you did the work is not a purchase"
    assert body["slot"]["state"] == "published"
    # This asserted "09:02" — the bug, written down as the specification.
    assert body["slot"]["published_at"] == "2026-08-03T09:02:00+05:30"
    assert body["streak"]["weeks"] == 7


def test_marking_published_twice_conflicts(client: TestClient) -> None:
    seed_with(mark_published(Slot("MON", monday()), at="2026-08-03T09:02:00+05:30"))
    res = client.post("/posts/week/MON/mark-published", json={}, headers=auth(make_token()))

    assert res.status_code == 409
    assert "Traceback" not in str(res.json())


def test_the_change_persists_across_requests(client: TestClient) -> None:
    seed_week()
    client.post("/posts/week/MON/mark-published", json={}, headers=auth(make_token()))
    body = client.get("/posts/week", headers=auth(make_token())).json()

    published = [s for s in body["slots"] if s["state"] == "published"]
    assert [s["day"] for s in published] == ["MON"]


def test_one_users_week_is_invisible_to_another(client: TestClient) -> None:
    STORE.save_week(
        "other-user-id",
        WeekPlan(
            week_start=monday(),
            slots=(mark_published(Slot("MON", monday()), at="2026-08-03T09:02:00+05:30"),),
        ),
    )
    body = client.get("/posts/week", headers=auth(make_token())).json()
    assert all(s["state"] != "published" for s in body["slots"])


# ── Skipping ───────────────────────────────────────────────────────────────


def test_skipping_is_free_and_holds_the_streak(client: TestClient) -> None:
    seed_with(Slot("MON", monday()), Slot("THU", monday() + timedelta(days=3)), streak=6)
    res = client.post(
        "/posts/week/MON/skip", json={"reason": "no_time"}, headers=auth(make_token())
    )

    assert res.status_code == 200
    body = res.json()
    assert body["charged"] == 0
    assert body["streak"]["paused"] is True
    assert body["streak"]["weeks"] == 6, "a skip must never reset the streak"


def test_the_skip_response_says_whether_it_retrains(client: TestClient) -> None:
    seed_week()
    weak = client.post(
        "/posts/week/MON/skip", json={"reason": "drafts_weak"}, headers=auth(make_token())
    ).json()
    assert weak["retrains"] is True

    # A fresh store rather than a reset: the second half of this test needs a
    # week with no skip on it, and clearing is what the old module dict offered.
    STORE.slot_rows.clear()
    STORE.week_rows.clear()
    seed_week()
    away = client.post(
        "/posts/week/MON/skip", json={"reason": "no_time"}, headers=auth(make_token())
    ).json()
    assert away["retrains"] is False


def test_a_committed_slot_cannot_be_skipped_without_a_reason(client: TestClient) -> None:
    seed_week()
    res = client.post("/posts/week/MON/skip", json={}, headers=auth(make_token()))

    assert res.status_code == 400
    assert res.json()["detail"]["error"] == "reason_required"
    # The message explains the benefit rather than scolding.
    assert "get better" in res.json()["detail"]["message"]


def test_an_optional_slot_skips_with_no_reason(client: TestClient) -> None:
    seed_week()
    res = client.post("/posts/week/SAT/skip", json={}, headers=auth(make_token()))

    assert res.status_code == 200
    assert res.json()["slot"]["state"] == "skipped"
    assert res.json()["slot"]["skip_reason"] is None


def test_the_users_own_words_are_recorded_verbatim(client: TestClient) -> None:
    """The tone law governs copy CARIGMA writes, not what a user says about
    their own week.

    An earlier draft ran the banned-phrase guard over this field, which would
    have refused to record "I feel like I'm falling behind" — the product
    censoring the user to protect its own style guide. The guard belongs on
    product copy only.
    """
    seed_week()
    note = "Travelling, and honestly I feel like I'm falling behind."
    res = client.post(
        "/posts/week/MON/skip",
        json={"reason": "no_time", "note": note},
        headers=auth(make_token()),
    )

    assert res.status_code == 200
    assert res.json()["slot"]["skip_note"] == note


# ── Regenerating: Content Intelligence ─────────────────────────────────────

GROUNDED: dict[str, Any] = {
    "name": "Ravi Kumar",
    "currentRole": "Senior Auditor / Functional Business Analyst",
    "experience": "CGDA | Senior Auditor. Led the PFMS rollout across field offices.",
    "skills": "SQL, Power BI, requirements analysis",
    "keyAchievement": "Cut monthly reconciliation time by 40%.",
}


def _post(lead: str, extra: str = "") -> str:
    filler = " ".join(f"word{i}" for i in range(90))
    return f"{lead}\n\nHere is what I mean. {filler} {extra}".strip()


class _Credits:
    def __init__(self) -> None:
        self.charged: list[tuple[str, int]] = []
        self.balance = 500

    def read_balance(self, _user_id: str) -> int:
        return self.balance

    def apply_delta(
        self, user_id: str, delta: int, reason: str, balance_after: int | None = None, **_kw: Any
    ) -> int:
        # The REAL signature. `charge_for_result` passes the new balance as a
        # fourth positional argument, and a fake that cannot take it raises —
        # which `charge_for_result` catches and logs as "delivering unbilled".
        # The jobs fake had exactly that defect, so its "charges once" test
        # passed while nothing was ever charged.
        self.charged.append((reason, delta))
        self.balance += delta
        return self.balance


class _Profiles:
    profile: dict[str, Any] = GROUNDED
    fails = False

    def __init__(self, _client: Any) -> None: ...

    def load(self, _user_id: str) -> dict[str, Any]:
        if _Profiles.fails:
            raise RuntimeError("profiles is down")
        return dict(_Profiles.profile)


class _Writer:
    """What the model said, and what it was asked."""

    def __init__(self) -> None:
        self.reply: Any = {
            "drafts": [
                {"angle": "Story", "body": _post("The audit that taught me to read data")},
                {"angle": "Practice", "body": _post("One SQL habit from audit work")},
                {"angle": "View", "body": _post("Dashboards are not the deliverable")},
            ]
        }
        self.prompts: list[str] = []
        self.dead = False

    def __call__(self, _system: str, user: str, _max: int) -> Any:
        self.prompts.append(user)
        if self.dead:
            from carigma_api.services.ai import UpstreamError

            raise UpstreamError("anthropic is down")
        return self.reply


@pytest.fixture
def drafting(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[_Credits, _Writer]]:
    credits, writer = _Credits(), _Writer()
    _Profiles.profile, _Profiles.fails = GROUNDED, False
    monkeypatch.setattr(posts_routes, "SupabaseCreditStore", lambda client: credits)
    monkeypatch.setattr(posts_routes, "ProfileRepository", _Profiles)
    monkeypatch.setattr(posts_routes, "_writer", lambda settings: writer)
    yield credits, writer


def test_a_slot_gets_three_drafts_and_is_charged_once(client: TestClient, drafting) -> None:  # type: ignore[no-untyped-def]
    """The line that raised "not wired up yet", wired."""
    credits, _writer = drafting
    seed_week()

    res = client.post("/posts/week/THU/regenerate", headers=auth(make_token()))

    assert res.status_code == 200
    slot = res.json()["slot"]
    assert slot["state"] == "draft_ready"
    assert [d["note"] for d in slot["drafts"]] == ["Story", "Practice", "View"]
    assert credits.charged == [("Regenerate one post", -3)]
    # And it is stored, not only returned.
    stored = STORE.load(USER_ID, monday())
    assert stored is not None
    assert len(next(s for s in stored.slots if s.day == "THU").drafts) == 3


def test_drafts_that_invent_or_leave_blanks_are_not_kept_or_charged(
    client: TestClient, drafting
) -> None:  # type: ignore[no-untyped-def]
    """The drafts go out under a real name. A figure the profile does not
    contain, or a blank to fill, is the model's failure — not a purchase."""
    credits, writer = drafting
    writer.reply = {
        "drafts": [
            {"angle": "Story", "body": _post("How we grew revenue", "It rose 63%.")},
            {"angle": "View", "body": _post("When I joined [Company]")},
        ]
    }
    seed_week()

    res = client.post("/posts/week/THU/regenerate", headers=auth(make_token()))

    assert res.status_code == 503
    assert res.json()["detail"]["error"] == "nothing_usable"
    assert "weren't charged" in res.json()["detail"]["message"]
    assert credits.charged == []
    stored = STORE.load(USER_ID, monday())
    assert stored is not None
    assert next(s for s in stored.slots if s.day == "THU").drafts == ()


def test_one_honest_draft_is_kept_when_the_others_are_not(client: TestClient, drafting) -> None:  # type: ignore[no-untyped-def]
    credits, writer = drafting
    writer.reply = {
        "drafts": [
            {"angle": "Story", "body": _post("Reconciliation, and the 40% we got back", "By 40%.")},
            {"angle": "View", "body": _post("Growth", "We grew 300%.")},
        ]
    }
    seed_week()

    res = client.post("/posts/week/THU/regenerate", headers=auth(make_token()))

    assert res.status_code == 200
    assert [d["note"] for d in res.json()["slot"]["drafts"]] == ["Story"]
    assert credits.charged == [("Regenerate one post", -3)]


def test_a_dead_model_charges_nothing_and_says_so(client: TestClient, drafting) -> None:  # type: ignore[no-untyped-def]
    credits, writer = drafting
    writer.dead = True
    seed_week()

    res = client.post("/posts/week/THU/regenerate", headers=auth(make_token()))

    assert res.status_code == 503
    assert res.json()["detail"]["error"] == "upstream_unavailable"
    assert "weren't charged" in res.json()["detail"]["message"]
    assert credits.charged == []
    assert "Traceback" not in str(res.json())


def test_nothing_to_write_from_is_refused_before_credits_or_the_model(
    client: TestClient, drafting
) -> None:  # type: ignore[no-untyped-def]
    """A profile with only a job title would get a stranger's career."""
    credits, writer = drafting
    _Profiles.profile = {"name": "New User", "currentRole": "Analyst"}
    seed_week()

    res = client.post("/posts/week/THU/regenerate", headers=auth(make_token()))

    assert res.status_code == 422
    assert res.json()["detail"]["error"] == "nothing_to_write_from"
    assert credits.charged == []
    assert writer.prompts == []


def test_an_unreadable_profile_is_not_an_empty_one(client: TestClient, drafting) -> None:  # type: ignore[no-untyped-def]
    credits, writer = drafting
    _Profiles.fails = True
    seed_week()

    res = client.post("/posts/week/THU/regenerate", headers=auth(make_token()))

    assert res.status_code == 503
    assert res.json()["detail"]["error"] == "profile_unavailable"
    assert credits.charged == []
    assert writer.prompts == []


def test_a_skip_that_retrains_reaches_the_next_draft(client: TestClient, drafting) -> None:  # type: ignore[no-untyped-def]
    """Declining is free BECAUSE it teaches. If the reason never reached the
    writer, the skip would be a form that goes nowhere."""
    _credits, writer = drafting
    from carigma_api.services.posts import SkipReason, skip

    seed_with(
        skip(
            Slot("MON", monday()),
            reason=SkipReason.DRAFTS_WEAK,
            note="too corporate, nothing like me",
        ),
        Slot("THU", monday() + timedelta(days=3)),
    )

    client.post("/posts/week/THU/regenerate", headers=auth(make_token()))

    assert "too corporate, nothing like me" in writer.prompts[0]


def test_regenerating_a_published_slot_is_refused_before_any_work(
    client: TestClient,
    drafting,  # type: ignore[no-untyped-def]
) -> None:
    credits, writer = drafting
    seed_with(
        mark_published(Slot("THU", monday() + timedelta(days=3)), at="2026-08-06T09:04:00+05:30")
    )
    res = client.post("/posts/week/THU/regenerate", headers=auth(make_token()))

    assert res.status_code == 409
    assert res.json()["detail"]["error"] == "already_published"
    assert credits.charged == []
    assert writer.prompts == []


def test_regeneration_requires_a_verified_token(client: TestClient) -> None:
    assert client.post("/posts/week/THU/regenerate").status_code == 401


# ── Marking published: the write Postgres refused ─────────────────────────


def test_marking_published_stores_a_moment_the_column_can_hold(client: TestClient) -> None:
    """Every "Mark published" in V2 failed: "13:05" into a timestamptz."""
    seed_with(
        Slot(
            "THU",
            monday() + timedelta(days=3),
            state=SlotState.DRAFT_READY,
            drafts=(Draft(index=1, body="A post."),),
        )
    )

    res = client.post("/posts/week/THU/mark-published", json={}, headers=auth(make_token()))

    assert res.status_code == 200
    stamped = res.json()["slot"]["published_at"]
    moment = datetime.fromisoformat(stamped)
    assert moment.tzinfo is not None, "a moment without a zone is a guess about where"
    assert abs((datetime.now(UTC) - moment).total_seconds()) < 60
    stored = STORE.load(USER_ID, monday())
    assert stored is not None
    assert next(s for s in stored.slots if s.day == "THU").state is SlotState.PUBLISHED


def test_a_time_the_user_gives_is_kept_and_one_we_cannot_read_is_refused(
    client: TestClient,
) -> None:
    seed_week()
    ok = client.post(
        "/posts/week/THU/mark-published",
        json={"at": "2026-10-18T09:30:00+05:30"},
        headers=auth(make_token()),
    )
    assert ok.status_code == 200
    assert ok.json()["slot"]["published_at"] == "2026-10-18T09:30:00+05:30"

    seed_week()
    bad = client.post(
        "/posts/week/THU/mark-published", json={"at": "13:05"}, headers=auth(make_token())
    )
    assert bad.status_code == 422
    assert bad.json()["detail"]["error"] == "bad_time"


def test_a_refused_write_is_said_not_reported_as_a_crash(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_week()

    def refuse(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("db down")

    monkeypatch.setattr(STORE, "save_slot", refuse)
    res = client.post("/posts/week/THU/mark-published", json={}, headers=auth(make_token()))

    assert res.status_code == 503
    assert res.json()["detail"]["error"] == "not_saved"
