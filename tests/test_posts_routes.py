"""Posts over HTTP.

The endpoint surface is where the "we never auto-post" rule becomes checkable
by someone who never reads the service layer: there is no publish endpoint, and
the only thing that advances the streak is the user saying they shipped it.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta

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
from tests.conftest import USER_ID, auth, make_token


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    posts_routes.reset_store()
    yield
    posts_routes.reset_store()


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
    posts_routes.seed(USER_ID, plan)
    return plan


def seed_with(*slots: Slot, streak: int = 0) -> None:
    posts_routes.seed(
        USER_ID,
        WeekPlan(week_start=monday(), slots=slots, streak_weeks=streak),
    )


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
    assert body["provenance"]["line"] == "drafts land 07:00 on slot days"


def test_a_paused_week_is_an_honest_nothing_not_an_empty_list(
    client: TestClient,
) -> None:
    posts_routes.seed(
        USER_ID,
        WeekPlan(week_start=monday(), slots=(), cadence_per_week=0, streak_weeks=6),
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
    assert [s["step"] for s in body["flow"]] == [1, 2, 3, 4]
    assert "you say so" in body["flow"][2]["label"]


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
        Slot("THU", monday() + timedelta(days=3), state=SlotState.PUBLISHED, published_at="09:04"),
        streak=6,
    )
    res = client.post(
        "/posts/week/MON/mark-published", json={"at": "09:02"}, headers=auth(make_token())
    )

    assert res.status_code == 200
    body = res.json()
    assert body["charged"] == 0, "telling us you did the work is not a purchase"
    assert body["slot"]["state"] == "published"
    assert body["slot"]["published_at"] == "09:02"
    assert body["streak"]["weeks"] == 7


def test_marking_published_twice_conflicts(client: TestClient) -> None:
    seed_with(mark_published(Slot("MON", monday()), at="09:02"))
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
    posts_routes.seed(
        "other-user-id",
        WeekPlan(week_start=monday(), slots=(mark_published(Slot("MON", monday()), at="09:02"),)),
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

    posts_routes.reset_store()
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


# ── Regenerating: the credit rule ──────────────────────────────────────────


def test_a_failed_regeneration_charges_nothing(client: TestClient) -> None:
    """Content Intelligence is not wired up yet, so this genuinely fails —
    which makes it a real test of the rule rather than a simulated one."""
    seed_week()
    res = client.post("/posts/week/THU/regenerate", headers=auth(make_token()))

    assert res.status_code in (402, 503)
    if res.status_code == 503:
        assert "weren't charged" in res.json()["detail"]["message"]
    assert "Traceback" not in str(res.json())


def test_regenerating_a_published_slot_is_refused_before_any_work(
    client: TestClient,
) -> None:
    seed_with(mark_published(Slot("THU", monday() + timedelta(days=3)), at="09:04"))
    res = client.post("/posts/week/THU/regenerate", headers=auth(make_token()))

    assert res.status_code in (402, 409)
    if res.status_code == 409:
        assert res.json()["detail"]["error"] == "already_published"


def test_regeneration_requires_a_verified_token(client: TestClient) -> None:
    assert client.post("/posts/week/THU/regenerate").status_code == 401
