"""The weekly contract over HTTP.

`test_weekly_contract.py` proves the state machine. This file proves the wire:
that the endpoint enforces the same rules, that another user's week is
unreachable, and — the one that matters most — that no request shape can
produce `state = approved` without a real approval.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from carigma_api.routes import weekly as weekly_routes
from carigma_api.services.review import Direction, ReviewFact, Streak
from carigma_api.services.weekly import (
    ContractItem,
    ContractState,
    WeeklyContract,
    auto_adopt,
)
from carigma_api.services.weekly_store import SupabaseContractStore
from tests.conftest import USER_ID, auth, make_token
from tests.fake_supabase import FakeDB

#: The store the routes are wired to for the duration of a test. Module-level
#: so `seed_week` can reach it without every test threading it through.
_DB: FakeDB


@pytest.fixture(autouse=True)
def _wired(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Real store code, in-memory rows.

    The routes used to hold contracts in a module dict, so the tests reset it
    between cases. They now go through `SupabaseContractStore`, which means
    every test here exercises the row round-trip — `to_row` writing what
    `from_row` reads — instead of putting a dataclass in and getting the same
    object back.
    """
    global _DB
    _DB = FakeDB()
    store = SupabaseContractStore(_DB)
    monkeypatch.setattr(weekly_routes, "_deps", lambda request, settings: store)
    yield


def this_monday() -> date:
    today = datetime.now(UTC).date()
    return today - timedelta(days=today.weekday())


def items() -> tuple[ContractItem, ...]:
    return (
        ContractItem("content", "MON", "Post slot 09:00", 10, 12),
        ContractItem("jobs", "WED", "Career Scout scan", 5, 15),
        ContractItem("review", "SUN", "Review, 18:00", 10, 0),
    )


def seed_week(user_id: str = USER_ID, **kw: object) -> WeeklyContract:
    contract = WeeklyContract(week_start=this_monday(), items=items(), **kw)  # type: ignore[arg-type]
    SupabaseContractStore(_DB).save(user_id, contract)
    return contract


# ── Reading ────────────────────────────────────────────────────────────────


def test_the_contract_requires_a_verified_token(client: TestClient) -> None:
    assert client.get("/weekly/contract").status_code == 401


def test_a_user_with_no_history_gets_a_promise_not_a_blank(client: TestClient) -> None:
    """Brief 2 day-one grammar: what will exist + when. Never an empty surface."""
    res = client.get("/weekly/contract", headers=auth(make_token()))

    assert res.status_code == 200
    body = res.json()
    assert body["contract"] is None
    # The EmptyHonest spine: happened / why / next — all three required.
    assert set(body["day_one"]) == {"happened", "why", "next"}
    assert "Sunday" in body["day_one"]["next"]


def test_the_proposed_week_comes_back_with_its_rows(client: TestClient) -> None:
    seed_week()
    body = client.get("/weekly/contract", headers=auth(make_token())).json()

    assert body["contract"]["state"] == "proposed"
    assert len(body["contract"]["items"]) == 3
    assert body["contract"]["total_minutes"] == 25


def test_one_users_week_is_invisible_to_another(client: TestClient) -> None:
    seed_week(user_id="other-user-id")
    body = client.get("/weekly/contract", headers=auth(make_token())).json()
    assert body["contract"] is None


# ── Signing ────────────────────────────────────────────────────────────────


def test_approving_returns_the_signature_confirmation(client: TestClient) -> None:
    seed_week()
    res = client.post(
        "/weekly/contract/approve",
        json={"week_start": this_monday().isoformat()},
        headers=auth(make_token()),
    )

    assert res.status_code == 200
    body = res.json()
    assert body["contract"]["state"] == "approved"
    assert body["contract"]["user_approved"] is True
    assert body["confirmation"]["message"] == "See you Monday."
    assert body["confirmation"]["decided_at"]


def test_partial_approval_is_honoured_over_the_wire(client: TestClient) -> None:
    seed_week()
    body = client.post(
        "/weekly/contract/approve",
        json={"week_start": this_monday().isoformat(), "accept": ["content", "review"]},
        headers=auth(make_token()),
    ).json()

    assert set(body["contract"]["accepted_kinds"]) == {"content", "review"}
    assert "jobs" not in body["contract"]["accepted_kinds"]


def test_approving_an_empty_list_records_a_decline(client: TestClient) -> None:
    seed_week()
    body = client.post(
        "/weekly/contract/approve",
        json={"week_start": this_monday().isoformat(), "accept": []},
        headers=auth(make_token()),
    ).json()

    assert body["contract"]["state"] == "declined"
    assert body["contract"]["user_approved"] is False


def test_signing_twice_conflicts_instead_of_overwriting_the_record(
    client: TestClient,
) -> None:
    seed_week()
    payload = {"week_start": this_monday().isoformat()}
    first = client.post("/weekly/contract/approve", json=payload, headers=auth(make_token()))
    second = client.post("/weekly/contract/approve", json=payload, headers=auth(make_token()))

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["detail"]["error"] == "already_decided"


def test_a_refusal_carries_no_stack_trace(client: TestClient) -> None:
    """Bible §18.1: users never see raw exceptions."""
    seed_week()
    payload = {"week_start": this_monday().isoformat()}
    client.post("/weekly/contract/approve", json=payload, headers=auth(make_token()))
    body = client.post("/weekly/contract/approve", json=payload, headers=auth(make_token())).json()

    text = str(body)
    assert "Traceback" not in text
    assert "ContractTransitionError" not in text
    assert body["detail"]["message"] == "That week has already been settled."


def test_approving_a_week_that_does_not_exist_says_so(client: TestClient) -> None:
    res = client.post(
        "/weekly/contract/approve",
        json={"week_start": "2026-01-05"},
        headers=auth(make_token()),
    )
    assert res.status_code == 404
    assert res.json()["detail"]["error"] == "no_contract"


def test_one_user_cannot_sign_anothers_week(client: TestClient) -> None:
    seed_week(user_id="other-user-id")
    res = client.post(
        "/weekly/contract/approve",
        json={"week_start": this_monday().isoformat()},
        headers=auth(make_token()),
    )
    assert res.status_code == 404


# ── The distinction, over the wire ─────────────────────────────────────────


def test_an_auto_adopted_week_never_reports_itself_as_approved(
    client: TestClient,
) -> None:
    """The whole point of the module, checked at the boundary a client reads."""
    SupabaseContractStore(_DB).save(USER_ID, auto_adopt(seed_week(), now=datetime.now(UTC)))

    body = client.get("/weekly/contract", headers=auth(make_token())).json()

    assert body["contract"]["state"] == "auto_adopted"
    assert body["contract"]["user_approved"] is False
    assert body["contract"]["accepted_kinds"] == []


def test_an_auto_adopted_week_cannot_be_retroactively_approved(
    client: TestClient,
) -> None:
    """Otherwise a stray client retry would convert "we assumed" into "they
    agreed" — the exact rewrite of history this design forbids."""
    SupabaseContractStore(_DB).save(USER_ID, auto_adopt(seed_week(), now=datetime.now(UTC)))

    res = client.post(
        "/weekly/contract/approve",
        json={"week_start": this_monday().isoformat()},
        headers=auth(make_token()),
    )

    assert res.status_code == 409
    after = client.get("/weekly/contract", headers=auth(make_token())).json()
    assert after["contract"]["state"] == "auto_adopted"


def test_auto_adoption_pauses_drafts_but_keeps_scanning(client: TestClient) -> None:
    SupabaseContractStore(_DB).save(USER_ID, auto_adopt(seed_week(), now=datetime.now(UTC)))
    body = client.get("/weekly/contract", headers=auth(make_token())).json()

    kinds = {i["kind"] for i in body["contract"]["items"]}
    assert "content" not in kinds, "drafts must not pile up unattended"
    assert "jobs" in kinds, "scans continue"


# ── Lapse presentation ─────────────────────────────────────────────────────


def _lapsed_history(count: int) -> None:
    monday = this_monday()
    for i in range(count, 0, -1):
        week = monday - timedelta(weeks=i)
        SupabaseContractStore(_DB).save(
            USER_ID,
            WeeklyContract(week_start=week, items=items(), state=ContractState.AUTO_ADOPTED),
        )


def test_no_lapse_means_no_card(client: TestClient) -> None:
    res = client.get("/weekly/standing-by", headers=auth(make_token()))
    assert res.json() == {"presentation": "none", "lapsed_weeks": 0}


def test_one_lapse_is_a_banner_that_states_what_was_done(client: TestClient) -> None:
    _lapsed_history(1)
    body = client.get("/weekly/standing-by", headers=auth(make_token())).json()

    assert body["presentation"] == "banner"
    assert "Nothing piled up" in body["body"]
    labels = [a["label"] for a in body["actions"]]
    assert not any("fortnightly" in item.lower() for item in labels)


def test_two_lapses_offer_the_fortnightly_exit_ramp(client: TestClient) -> None:
    _lapsed_history(2)
    body = client.get("/weekly/standing-by", headers=auth(make_token())).json()

    assert body["presentation"] == "standing_by"
    assert body["lapsed_weeks"] == 2
    labels = [a["label"] for a in body["actions"]]
    assert any("fortnightly" in item.lower() for item in labels)


def test_the_lapsed_card_never_ships_a_shaming_string(client: TestClient) -> None:
    from carigma_api.services.weekly import assert_no_guilt

    _lapsed_history(3)
    body = client.get("/weekly/standing-by", headers=auth(make_token())).json()

    def strings(node: object) -> list[str]:
        if isinstance(node, str):
            return [node]
        if isinstance(node, dict):
            return [s for v in node.values() for s in strings(v)]
        if isinstance(node, list):
            return [s for v in node for s in strings(v)]
        return []

    assert_no_guilt(*strings(body))


# ── Re-entry ───────────────────────────────────────────────────────────────


def test_re_entry_proposes_a_lighter_week_that_still_needs_signing(
    client: TestClient,
) -> None:
    body = client.post("/weekly/contract/re-entry", headers=auth(make_token())).json()

    assert body["contract"]["state"] == "proposed"
    assert body["contract"]["user_approved"] is False
    assert body["contract"]["total_minutes"] <= 25


def test_re_entry_can_take_the_fortnightly_cadence(client: TestClient) -> None:
    body = client.post(
        "/weekly/contract/re-entry",
        json={"cadence": "fortnightly"},
        headers=auth(make_token()),
    ).json()

    assert body["contract"]["cadence"] == "fortnightly"


def test_re_entry_does_not_clobber_an_existing_week(client: TestClient) -> None:
    seed_week()
    res = client.post("/weekly/contract/re-entry", headers=auth(make_token()))

    assert res.status_code == 409
    assert res.json()["detail"]["error"] == "week_exists"


def test_a_re_entry_week_can_then_be_signed(client: TestClient) -> None:
    """The full loop: lapse -> pick up the thread -> sign."""
    client.post("/weekly/contract/re-entry", headers=auth(make_token()))
    res = client.post(
        "/weekly/contract/approve",
        json={"week_start": this_monday().isoformat()},
        headers=auth(make_token()),
    )

    assert res.status_code == 200
    assert res.json()["contract"]["user_approved"] is True


# ── GET /weekly/review ─────────────────────────────────────────────────────
# This endpoint had no test of any kind. It was not on the UNBUILT list — the
# route existed and returned 200 — which is exactly what UNVERIFIED is for.


def last_monday() -> date:
    return this_monday() - timedelta(weeks=1)


def store() -> SupabaseContractStore:
    return SupabaseContractStore(_DB)


def a_fact(text: str = "3 posts shipped") -> ReviewFact:
    return ReviewFact(direction=Direction.UP, text=text, receipt="content_loop, 3 rows")


def test_the_review_requires_a_verified_token(client: TestClient) -> None:
    assert client.get("/weekly/review").status_code == 401


def test_a_user_with_no_review_gets_null_not_an_error(client: TestClient) -> None:
    """Day one. Null is the honest answer and the client renders the promise."""
    res = client.get("/weekly/review", headers=auth(make_token()))

    assert res.status_code == 200
    assert res.json() == {"review": None}


def test_a_built_review_comes_back_with_its_facts(client: TestClient) -> None:
    store().save_review(
        USER_ID,
        last_monday(),
        weekly_routes.earned_review(facts=(a_fact(),), streak=Streak(weeks=4)),
    )

    review = client.get("/weekly/review", headers=auth(make_token())).json()["review"]

    assert review is not None
    assert [f["text"] for f in review["facts"]] == ["3 posts shipped"]
    assert review["streak"]["weeks"] == 4
    assert review["week_label"].startswith("Week of ")


def test_a_quiet_week_is_a_review_with_no_facts_not_a_missing_review(
    client: TestClient,
) -> None:
    """The absence question, on this surface.

    A week that produced nothing is a REAL answer about a real week. Rendered
    as "your first review builds Sunday" it would tell someone who has been
    here four months that they are new — the same collapse the Posts port hit,
    where a paused week read as never-planned.
    """
    store().save_review(
        USER_ID, last_monday(), weekly_routes.earned_review(facts=(), streak=Streak(weeks=0))
    )

    review = client.get("/weekly/review", headers=auth(make_token())).json()["review"]

    assert review is not None, "a quiet week is not a missing week"
    assert review["facts"] == []
    assert review["streak"]["weeks"] == 0


def test_this_weeks_row_is_not_read_as_last_weeks_review(client: TestClient) -> None:
    """The review covers the week that CLOSED.

    This week's row holds the plan being proposed; last week's holds what
    happened. They share a screen and a table, and reading the wrong one would
    report a week that has not finished yet.
    """
    seed_week()  # this week's contract, with no facts on it
    store().save_review(
        USER_ID,
        last_monday(),
        weekly_routes.earned_review(facts=(a_fact("last week"),), streak=Streak(weeks=1)),
    )

    review = client.get("/weekly/review", headers=auth(make_token())).json()["review"]

    assert review is not None
    assert [f["text"] for f in review["facts"]] == ["last week"]


def test_a_review_we_cannot_read_is_not_reported_as_absent(client: TestClient) -> None:
    """ "We couldn't look" is not "there is nothing".

    Returning null on a failed read would render the day-one promise to a
    long-standing user — a claim about their history, made from an error.
    """
    _DB.failing.add("weekly_review")

    res = client.get("/weekly/review", headers=auth(make_token()))

    assert res.status_code == 503
    assert res.json()["detail"]["error"] == "read_failed"
    assert "Traceback" not in res.text


def test_saving_a_review_does_not_blank_the_contract_on_that_row(
    client: TestClient,
) -> None:
    """One row carries both. A review written for a week that already has a
    decided contract must not reset its state — an upsert that replaced the
    row would silently reopen a week the user had signed."""
    signed = WeeklyContract(
        week_start=last_monday(),
        items=items(),
        state=ContractState.APPROVED,
        accepted_kinds=("content",),
        decided_at=datetime.now(UTC),
    )
    store().save(USER_ID, signed)

    store().save_review(
        USER_ID, last_monday(), weekly_routes.earned_review(facts=(a_fact(),), streak=Streak(1))
    )

    after = store().load(USER_ID, last_monday())
    assert after is not None
    assert after.state is ContractState.APPROVED
    assert after.accepted_kinds == ("content",)
    assert after.decided_at is not None


# ── The absence question, through the database ─────────────────────────────


def test_took_nothing_and_never_decided_survive_the_round_trip(
    client: TestClient,
) -> None:
    """Both have empty `accepted_kinds`, and the row must still tell them apart.

    Collapsing them would let the Sunday sweep auto-adopt a week the user
    explicitly declined — putting a plan in force that they refused.
    """
    monday = this_monday()
    declined = WeeklyContract(
        week_start=monday,
        items=items(),
        state=ContractState.DECLINED,
        accepted_kinds=(),
        decided_at=datetime.now(UTC),
    )
    open_week = WeeklyContract(
        week_start=monday - timedelta(weeks=1),
        items=items(),
        state=ContractState.PROPOSED,
        accepted_kinds=(),
    )
    store().save(USER_ID, declined)
    store().save(USER_ID, open_week)

    back = {c.week_start: c for c in store().history(USER_ID)}

    took_nothing = back[monday]
    never_decided = back[monday - timedelta(weeks=1)]

    assert took_nothing.accepted_kinds == never_decided.accepted_kinds == ()
    assert took_nothing.state is ContractState.DECLINED
    assert took_nothing.decided_at is not None
    assert never_decided.state is ContractState.PROPOSED
    assert never_decided.decided_at is None
    # And the thing that actually matters: neither puts anything in force, but
    # only one of them is still open to being answered.
    assert took_nothing.state.is_decided is True
    assert never_decided.state.is_decided is False


def test_a_row_with_an_unreadable_state_raises_rather_than_reopening_the_week(
    client: TestClient,
) -> None:
    """`or "proposed"` used to make this silently return an open proposal."""
    _DB.seed(
        "weekly_review",
        {
            "user_id": USER_ID,
            "week_start": this_monday().isoformat(),
            "state": "",
            "cadence": "weekly",
            "items": [],
            "accepted_kinds": [],
        },
    )

    with pytest.raises(ValueError):
        store().history(USER_ID)


def test_the_build_time_is_when_the_review_was_built(client: TestClient) -> None:
    """Not when the row appeared.

    The row is created when the week's CONTRACT is proposed, on the Monday;
    the review is built the following Sunday. Reading `created_at` would have
    reported every review as built six days before it was — and the version
    before that printed the literal string "Sun 18:00" regardless.
    """
    built = datetime(2026, 8, 9, 18, 4, tzinfo=UTC)
    # The contract row for that week exists first, as it does in real life.
    store().save(USER_ID, WeeklyContract(week_start=last_monday(), items=items()))
    store().save_review(
        USER_ID,
        last_monday(),
        weekly_routes.earned_review(facts=(a_fact(),), streak=Streak(1), built_at=built),
    )

    review = client.get("/weekly/review", headers=auth(make_token())).json()["review"]

    assert review["built_at"] == built.isoformat()
    assert "18:00" not in review["built_at"], "the hardcoded Sunday claim is gone"
