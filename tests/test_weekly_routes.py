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
from carigma_api.services.weekly import (
    ContractItem,
    ContractState,
    WeeklyContract,
    auto_adopt,
)
from tests.conftest import USER_ID, auth, make_token


@pytest.fixture(autouse=True)
def _clean_store() -> Iterator[None]:
    weekly_routes.reset_store()
    yield
    weekly_routes.reset_store()


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
    weekly_routes.seed(user_id, contract)
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
    weekly_routes.seed(USER_ID, auto_adopt(seed_week(), now=datetime.now(UTC)))

    body = client.get("/weekly/contract", headers=auth(make_token())).json()

    assert body["contract"]["state"] == "auto_adopted"
    assert body["contract"]["user_approved"] is False
    assert body["contract"]["accepted_kinds"] == []


def test_an_auto_adopted_week_cannot_be_retroactively_approved(
    client: TestClient,
) -> None:
    """Otherwise a stray client retry would convert "we assumed" into "they
    agreed" — the exact rewrite of history this design forbids."""
    weekly_routes.seed(USER_ID, auto_adopt(seed_week(), now=datetime.now(UTC)))

    res = client.post(
        "/weekly/contract/approve",
        json={"week_start": this_monday().isoformat()},
        headers=auth(make_token()),
    )

    assert res.status_code == 409
    after = client.get("/weekly/contract", headers=auth(make_token())).json()
    assert after["contract"]["state"] == "auto_adopted"


def test_auto_adoption_pauses_drafts_but_keeps_scanning(client: TestClient) -> None:
    weekly_routes.seed(USER_ID, auto_adopt(seed_week(), now=datetime.now(UTC)))
    body = client.get("/weekly/contract", headers=auth(make_token())).json()

    kinds = {i["kind"] for i in body["contract"]["items"]}
    assert "content" not in kinds, "drafts must not pile up unattended"
    assert "jobs" in kinds, "scans continue"


# ── Lapse presentation ─────────────────────────────────────────────────────


def _lapsed_history(count: int) -> None:
    monday = this_monday()
    for i in range(count, 0, -1):
        week = monday - timedelta(weeks=i)
        weekly_routes.seed(
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
