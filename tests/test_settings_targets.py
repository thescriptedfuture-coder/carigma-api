"""Targets and cadence, after onboarding.

Set once at onboarding and unchangeable after it — while the cadence became
the days every weekly plan proposes (S8, S9). A user who chose five posts a
week and wanted three had no way to say so.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from carigma_api.routes import settings as settings_routes
from carigma_api.services.repository import db_to_profile, profile_to_db
from tests.conftest import auth, make_token


class RoundTrip:
    """Saves through the REAL `profile_to_db` and loads through the REAL
    `db_to_profile`, so a key the table cannot hold fails here, and a read-back
    is what the row would say — not what was asked for."""

    def __init__(self, profile: dict[str, Any] | None = None) -> None:
        self.columns: dict[str, Any] = profile_to_db(profile or {})
        self.fail_save = False
        self.fail_load = False
        self.drop_on_save: set[str] = set()

    def load(self, user_id: str) -> dict[str, Any]:
        if self.fail_load:
            raise RuntimeError("db down")
        return db_to_profile(dict(self.columns))

    def save(self, user_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        if self.fail_save:
            raise RuntimeError("db down")
        written = profile_to_db(patch)
        self.columns.update({k: v for k, v in written.items() if k not in self.drop_on_save})
        return self.load(user_id)


@pytest.fixture
def row(monkeypatch: pytest.MonkeyPatch) -> RoundTrip:
    fake = RoundTrip({"targetRoles": "Business Analyst", "cadence": 5, "platforms": ["linkedin"]})
    monkeypatch.setattr(settings_routes, "_profiles", lambda request, settings: (fake, None))
    return fake


def test_the_targets_come_back_with_every_cadence_and_its_days(
    client: TestClient, row: RoundTrip
) -> None:
    body = client.get("/profile/targets", headers=auth(make_token())).json()

    assert body["target_roles"] == "Business Analyst"
    assert body["cadence"] == 5
    assert body["options"] == [
        {"cadence": 2, "days": ["TUE", "THU"]},
        {"cadence": 3, "days": ["MON", "WED", "FRI"]},
        {"cadence": 5, "days": ["MON", "TUE", "WED", "THU", "FRI"]},
    ]


def test_changing_the_cadence_is_what_the_row_then_says(client: TestClient, row: RoundTrip) -> None:
    res = client.put(
        "/profile/targets",
        json={"target_roles": "MIS Analyst, Finance Analyst", "cadence": 3},
        headers=auth(make_token()),
    )

    assert res.status_code == 200
    stored = row.load("u")
    assert stored["cadence"] == 3
    assert stored["targetRoles"] == "MIS Analyst, Finance Analyst"
    # V1 reads the singular too.
    assert stored["targetRole"] == "MIS Analyst"
    assert res.json()["note"] == (
        "The next plan you are offered proposes 3 posts a week — Mon, Wed, Fri. "
        "A week you have already signed stays as you signed it."
    )


def test_the_next_plan_is_proposed_from_the_new_cadence(client: TestClient, row: RoundTrip) -> None:
    """The point of the setting: what Sunday and Plan this week offer."""
    from datetime import date

    from carigma_api.services.schedule import proposal_items

    client.put(
        "/profile/targets",
        json={"target_roles": "Business Analyst", "cadence": 2},
        headers=auth(make_token()),
    )

    items = proposal_items(row.load("u"), today=date(2026, 10, 26))
    assert [i.day for i in items if i.kind == "content"] == ["TUE", "THU"]


@pytest.mark.parametrize("cadence", [0, 1, 4, 7])
def test_a_cadence_we_do_not_offer_is_refused(
    client: TestClient, row: RoundTrip, cadence: int
) -> None:
    res = client.put(
        "/profile/targets",
        json={"target_roles": "Business Analyst", "cadence": cadence},
        headers=auth(make_token()),
    )

    assert res.status_code == 422
    assert row.load("u")["cadence"] == 5


def test_a_blank_target_is_refused(client: TestClient, row: RoundTrip) -> None:
    """Every agent aims at something; a blank target searches for nothing."""
    res = client.put(
        "/profile/targets",
        json={"target_roles": "   ", "cadence": 3},
        headers=auth(make_token()),
    )

    assert res.status_code == 422
    assert row.load("u")["targetRoles"] == "Business Analyst"


def test_a_save_that_did_not_stick_is_a_failed_save(client: TestClient, row: RoundTrip) -> None:
    """It would have said "the next plan proposes None posts a week"."""
    row.drop_on_save = {"cadence"}

    res = client.put(
        "/profile/targets",
        json={"target_roles": "Business Analyst", "cadence": 3},
        headers=auth(make_token()),
    )

    assert res.status_code == 503
    assert res.json()["detail"]["message"].endswith("Your settings are unchanged.")


def test_a_failed_save_says_nothing_changed(client: TestClient, row: RoundTrip) -> None:
    row.fail_save = True

    res = client.put(
        "/profile/targets",
        json={"target_roles": "Business Analyst", "cadence": 3},
        headers=auth(make_token()),
    )

    assert res.status_code == 503
    assert "Traceback" not in res.text


def test_an_unreadable_profile_is_a_503_not_empty_targets(
    client: TestClient, row: RoundTrip
) -> None:
    """Blank targets for "could not look" would invite someone to retype
    what they already set."""
    row.fail_load = True

    res = client.get("/profile/targets", headers=auth(make_token()))

    assert res.status_code == 503
    assert res.json()["detail"]["error"] == "read_failed"


def test_a_cadence_never_chosen_is_null_not_presented_as_a_choice(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = RoundTrip({"targetRoles": "Business Analyst"})
    monkeypatch.setattr(settings_routes, "_profiles", lambda request, settings: (fake, None))

    body = client.get("/profile/targets", headers=auth(make_token())).json()

    assert body["cadence"] is None
    assert body["default_cadence"] == 3
