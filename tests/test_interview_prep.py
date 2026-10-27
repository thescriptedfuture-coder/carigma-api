"""Interview prep, per application (S14).

V2's prep page read "Interview prep · not built yet", and Today's top card, an
interview inside 72 hours, opened an application with nothing to prepare from.
V1 built these kits from the tracker; `interview_preps` holds two of them.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from carigma_api.routes import agents as agents_routes
from carigma_api.routes import prep as routes
from carigma_api.services.agents import interview_coach
from carigma_api.services.run_store import SupabaseRunStore
from tests.conftest import USER_ID, auth, make_token
from tests.fake_supabase import FakeDB, FakeTable, Result
from tests.test_jobs_routes import _FakeCredits

APP = "a1b2c3d4-0000-4000-8000-000000000001"
OTHERS = "a1b2c3d4-0000-4000-8000-000000000099"

PROFILE = {
    "name": "Ravi Kumar",
    "currentRole": "Business Analyst",
    "experience": "Business Analyst at DTS, 2019-present. Built the ops reporting suite.",
    "skills": "SQL, Power BI",
}


def _question(n: int, **over: Any) -> dict[str, Any]:
    return {
        "question": f"Tell me about a time you used data to change a decision ({n}).",
        "category": "behavioural",
        "difficulty": "medium",
        "whyAsked": "Whether you connect analysis to outcomes.",
        "modelAnswer": f"At DTS I rebuilt the ops reporting suite ({n})...",
        "tips": ["Name the decision", "Say what changed"],
        **over,
    }


def _reply(questions: int = 8) -> dict[str, Any]:
    return {
        "role": "Product Analyst at Zomato",
        "overview": "Lead with the reporting suite.",
        "questions": [_question(i) for i in range(questions)],
        "questionsToAskThem": ["How is success measured in the first 90 days?"],
        "redFlags": ["Talking about tools instead of decisions"],
        "closingPitch": "I turn messy data into decisions.",
    }


class _Profiles:
    profile: dict[str, Any] = PROFILE
    fails = False

    def __init__(self, _client: Any) -> None:
        pass

    def load(self, _user_id: str) -> dict[str, Any]:
        if self.fails:
            raise RuntimeError("profiles is down")
        return dict(self.profile)


@pytest.fixture
def prepping(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, FakeDB, _FakeCredits, dict[str, Any]]]:
    db = FakeDB()
    db.seed(
        "job_applications",
        {
            "id": APP,
            "user_id": USER_ID,
            "company": "Zomato",
            "role": "Product Analyst",
            "status": "interview",
            "interview_on": "2026-10-29",
        },
        {
            "id": OTHERS,
            "user_id": "someone-else",
            "company": "CRED",
            "role": "Analyst",
            "status": "interview",
        },
    )
    credits = _FakeCredits()
    state: dict[str, Any] = {"reply": _reply(), "dead": False, "prompts": []}

    def writer(system: str, user: str, _max: int) -> object:
        state["prompts"].append((system, user))
        if state["dead"]:
            from carigma_api.services.ai import UpstreamError

            raise UpstreamError("anthropic is down")
        return state["reply"]

    _Profiles.profile, _Profiles.fails = PROFILE, False
    monkeypatch.setattr(routes, "_db", lambda request, settings: db)
    monkeypatch.setattr(routes, "SupabaseCreditStore", lambda client: credits)
    monkeypatch.setattr(routes, "ProfileRepository", _Profiles)
    monkeypatch.setattr(routes, "_writer", lambda settings: writer)
    monkeypatch.setattr(agents_routes, "_store", lambda request, settings: SupabaseRunStore(db))
    yield client, db, credits, state


def _get(client: TestClient, app_id: str = APP) -> Any:
    return client.get(f"/tracker/{app_id}/prep", headers=auth(make_token()))


def _build(client: TestClient, app_id: str = APP, *, background: bool = False) -> Any:
    return client.post(
        f"/tracker/{app_id}/prep", json={"background": background}, headers=auth(make_token())
    )


# ── Reading ────────────────────────────────────────────────────────────────


def test_both_endpoints_require_a_verified_token(client: TestClient) -> None:
    assert client.get(f"/tracker/{APP}/prep").status_code == 401
    assert client.post(f"/tracker/{APP}/prep").status_code == 401


def test_an_application_with_no_kit_says_so_and_the_price(prepping: Any) -> None:
    client, _db, _credits, _state = prepping

    body = _get(client).json()

    assert body["prep"] is None
    assert body["built_at"] is None
    assert body["cost"] == 12


def test_a_kit_v1_built_opens_in_v2(prepping: Any) -> None:
    """V1 wrote `behavioral`, and a question with no model answer is not one."""
    client, db, _credits, _state = prepping
    v1_kit = _reply(2)
    v1_kit["questions"][0]["category"] = "behavioral"
    v1_kit["questions"].append({"question": "Why us?"})
    db.seed(
        "interview_preps",
        {
            "user_id": USER_ID,
            "application_id": APP,
            "prep": v1_kit,
            "created_at": "2026-09-30T10:00:00+00:00",
        },
    )

    body = _get(client).json()

    assert body["built_at"] == "2026-09-30T10:00:00+00:00"
    assert [q["category"] for q in body["prep"]["questions"]] == ["behavioural", "behavioural"]
    assert body["disclaimer"]


def test_someone_elses_application_is_not_found(prepping: Any) -> None:
    client, _db, _credits, _state = prepping

    assert _get(client, OTHERS).status_code == 404
    assert _build(client, OTHERS).status_code == 404


def test_a_kit_that_cannot_be_read_is_not_called_missing(prepping: Any) -> None:
    """The page would offer to build, and charge for, a kit they already have."""
    client, db, _credits, _state = prepping
    db.failing.add("interview_preps")

    assert _get(client).status_code == 503


# ── Building ───────────────────────────────────────────────────────────────


def test_a_kit_is_built_for_this_application_saved_and_charged_once(prepping: Any) -> None:
    client, db, credits, state = prepping

    res = _build(client)

    assert res.status_code == 200, res.text
    body = res.json()
    assert len(body["prep"]["questions"]) == 8
    assert body["built_at"]
    assert credits.charged == [("Interview Intelligence run", -12)]
    # V1's role line, and the person's own background in the prompt.
    _system, user = state["prompts"][0]
    assert "targeting: Product Analyst at Zomato" in user
    assert "Built the ops reporting suite" in user
    stored = db.rows("interview_preps")
    assert len(stored) == 1 and stored[0]["application_id"] == APP
    # The timeline says it happened, in V1's vocabulary.
    assert [e["event_type"] for e in db.rows("application_events")] == ["prep_built"]
    # And opening it again is free.
    assert _get(client).json()["prep"]["questions"] == body["prep"]["questions"]


def test_in_the_background_it_answers_at_once_and_the_poll_finds_the_kit(prepping: Any) -> None:
    client, _db, credits, _state = prepping

    res = _build(client, background=True)

    assert res.status_code == 202
    started = res.json()
    assert started["subject"] == {"application_id": APP}
    run = client.get(f"/agents/runs/{started['run_id']}", headers=auth(make_token())).json()
    assert run["status"] == "succeeded"
    assert len(run["result"]["prep"]["questions"]) == 8
    assert credits.charged == [("Interview Intelligence run", -12)]


def test_the_application_page_finds_a_prep_still_being_built(prepping: Any) -> None:
    from carigma_api.services.runs import RunStatus, new_run

    client, db, _credits, _state = prepping
    run = new_run(USER_ID, "interview")
    run.status, run.started_at = RunStatus.RUNNING, datetime.now(UTC)
    run.subject = {"application_id": APP}
    SupabaseRunStore(db).save(run)

    mine = client.get(
        f"/agents/runs/active?agent=interview&application_id={APP}", headers=auth(make_token())
    )
    other = client.get(
        f"/agents/runs/active?agent=interview&application_id={OTHERS}",
        headers=auth(make_token()),
    )

    assert mine.json()["run"]["run_id"] == run.id
    assert other.json()["run"] is None


def test_a_thin_kit_is_not_kept_and_not_charged(prepping: Any) -> None:
    client, db, credits, state = prepping
    state["reply"] = _reply(interview_coach.MIN_QUESTIONS - 1)

    res = _build(client)

    assert res.status_code == 503
    assert res.json()["detail"]["error"] == "nothing_usable"
    assert credits.charged == []
    assert db.rows("interview_preps") == []


def test_a_failed_rebuild_keeps_the_kit_already_there_and_is_free(prepping: Any) -> None:
    client, db, credits, state = prepping
    assert _build(client).status_code == 200
    first = _get(client).json()["prep"]
    state["dead"] = True

    res = _build(client)

    assert res.status_code == 503
    assert credits.charged == [("Interview Intelligence run", -12)]
    assert _get(client).json()["prep"] == first


def test_a_rebuild_replaces_the_kit_and_is_charged_again(prepping: Any) -> None:
    """V1's "Rebuild my prep", at the price on the button."""
    client, db, credits, state = prepping
    assert _build(client).status_code == 200
    state["reply"] = {**_reply(), "overview": "Lead with the pricing work."}

    assert _build(client).status_code == 200

    assert len(db.rows("interview_preps")) == 1
    assert _get(client).json()["prep"]["overview"] == "Lead with the pricing work."
    assert len(credits.charged) == 2


def test_no_experience_is_refused_before_credits_or_the_model(prepping: Any) -> None:
    client, _db, credits, state = prepping
    _Profiles.profile = {"name": "Ravi Kumar", "experience": "  "}

    res = _build(client)

    assert res.status_code == 422
    assert res.json()["detail"]["error"] == "nothing_to_prep_from"
    assert state["prompts"] == [] and credits.charged == []


def test_a_save_the_database_refuses_is_a_failed_run_and_free(
    prepping: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RLS refuses a write by touching nothing, without an error. An unchecked
    save would have charged 12 credits for a kit that was never kept."""
    client, db, credits, _state = prepping
    real = FakeTable.execute

    def refused(self: FakeTable) -> Any:
        if self._name == "interview_preps" and self._op == "upsert":
            return Result([])
        return real(self)

    monkeypatch.setattr(FakeTable, "execute", refused)

    res = _build(client)

    assert res.status_code == 503
    assert credits.charged == []


# ── The kit's shape ────────────────────────────────────────────────────────


def test_a_question_without_a_model_answer_is_dropped_and_odd_labels_left_out() -> None:
    raw = _reply(5)
    raw["questions"][0]["modelAnswer"] = ""
    raw["questions"][1]["category"] = "trick"
    raw["questions"][1]["difficulty"] = "impossible"

    kit = interview_coach.kit_from(raw, role="Analyst at CRED")

    assert kit is not None
    assert len(kit["questions"]) == 4
    assert kit["questions"][0]["category"] is None
    assert kit["questions"][0]["difficulty"] is None


def test_a_payload_that_is_not_an_object_is_nothing() -> None:
    assert interview_coach.kit_from(["not", "a", "kit"], role="x") is None
