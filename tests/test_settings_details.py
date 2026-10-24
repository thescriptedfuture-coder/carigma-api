"""The profile fields the agents read, editable in Settings (S13).

Tailored CVs read the name, current role, location, LinkedIn URL and
education; every draft reads the voice (tone, niche, personal brand, what to
avoid); the analyst and CVs read the brief; the download reads the default
template. V1 let a user edit all of it. V2 let them edit none of it after
onboarding.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from carigma_api.routes import settings as settings_routes
from tests.conftest import auth, make_token
from tests.test_settings_targets import RoundTrip

PROFILE = {
    "name": "Ravi Kumar",
    "currentRole": "Senior Auditor, CGDA",
    "location": "New Delhi",
    "targetRoles": "Business Analyst",
    "tone": "Storytelling & personal",
    "niche": "Government finance",
    "primaryGoal": "Move into analytics",
    "cvTemplate": "classic",
}


@pytest.fixture
def row(monkeypatch: pytest.MonkeyPatch) -> RoundTrip:
    fake = RoundTrip(PROFILE)
    monkeypatch.setattr(settings_routes, "_profiles", lambda request, settings: (fake, None))
    return fake


def _put(client: TestClient, **fields: Any) -> Any:
    return client.put("/profile/details", json=fields, headers=auth(make_token()))


def test_the_details_come_back_in_the_sections_settings_shows(
    client: TestClient, row: RoundTrip
) -> None:
    body = client.get("/profile/details", headers=auth(make_token())).json()

    assert body["about"]["name"] == "Ravi Kumar"
    assert body["about"]["current_role"] == "Senior Auditor, CGDA"
    assert body["voice"]["tone"] == "Storytelling & personal"
    assert body["brief"]["primary_goal"] == "Move into analytics"
    assert body["cv_template"] == "classic"
    assert body["options"]["tones"][0] == "Professional yet warm"
    assert [t["key"] for t in body["options"]["cv_templates"]] == [
        "modern",
        "classic",
        "executive",
        "minimal",
        "tech",
        "sidebar",
    ]


def test_a_section_saves_its_own_fields_and_leaves_the_rest(
    client: TestClient, row: RoundTrip
) -> None:
    res = _put(
        client,
        tone="Bold & opinionated",
        niche="Public finance and ERP",
        personal_brand="The auditor who reads dashboards",
        content_avoid="Politics",
    )

    assert res.status_code == 200
    stored = row.load("u")
    assert stored["tone"] == "Bold & opinionated"
    assert stored["personalBrand"] == "The auditor who reads dashboards"
    # Untouched by a voice save.
    assert stored["name"] == "Ravi Kumar"
    assert stored["cvTemplate"] == "classic"
    assert res.json()["note"].startswith("Saved.")


def test_what_is_saved_is_what_the_writer_reads(client: TestClient, row: RoundTrip) -> None:
    """The point: the next draft uses it."""
    from carigma_api.services import content_writer
    from carigma_api.services.posts import Slot

    _put(client, tone="Educational & formal", content_avoid="Salary talk")
    profile = {
        **row.load("u"),
        "experience": "Ten years auditing government accounts.",
        "skills": "SQL",
    }

    from datetime import date

    from carigma_api.services.posts import WeekPlan

    brief = content_writer.brief_for(
        profile, Slot("THU", date(2026, 10, 29)), week=WeekPlan(week_start=date(2026, 10, 26))
    )

    assert brief.tone == "Educational & formal"


def test_an_empty_string_clears_an_optional_field(client: TestClient, row: RoundTrip) -> None:
    _put(client, niche="")

    assert not row.load("u").get("niche")


def test_the_name_cannot_be_cleared(client: TestClient, row: RoundTrip) -> None:
    """It heads every CV."""
    res = _put(client, name="   ")

    assert res.status_code == 422
    assert row.load("u")["name"] == "Ravi Kumar"


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/ravi",
        "https://www.linkedin.com/company/carigma",
        "https://www.linkedin.com/jobs/view/4412",
        "javascript:alert(1)",
        "ravi-kumar-analytics",
    ],
)
def test_what_is_not_a_profile_address_is_refused(
    client: TestClient, row: RoundTrip, url: str
) -> None:
    assert _put(client, linkedin_url=url).status_code == 422


@pytest.mark.parametrize(
    "pasted",
    [
        # What Ravi pasted from his browser (UAT, 24 Oct) — refused, because
        # the check required `https://`. The test beside it ASSERTED that
        # `linkedin.com/in/ravi` must be refused: it pinned the bug.
        "www.linkedin.com/in/ravi-kumar-analytics",
        "linkedin.com/in/ravi-kumar-analytics",
        "https://www.linkedin.com/in/ravi-kumar-analytics/",
        "http://in.linkedin.com/in/ravi-kumar-analytics?utm_source=share",
        "  HTTPS://WWW.LINKEDIN.COM/in/ravi-kumar-analytics  ",
    ],
)
def test_a_profile_address_is_kept_in_one_form_however_it_was_pasted(
    client: TestClient, row: RoundTrip, pasted: str
) -> None:
    res = _put(client, linkedin_url=pasted)

    assert res.status_code == 200, res.text
    assert row.load("u")["linkedinUrl"] == "https://www.linkedin.com/in/ravi-kumar-analytics"
    assert res.json()["about"]["linkedin_url"] == "https://www.linkedin.com/in/ravi-kumar-analytics"


def test_a_tone_or_template_we_do_not_offer_is_refused(client: TestClient, row: RoundTrip) -> None:
    assert _put(client, tone="Sarcastic").status_code == 422
    assert _put(client, cv_template="comic-sans").status_code == 422
    assert row.load("u")["tone"] == "Storytelling & personal"


def test_a_value_too_long_is_refused_not_cut(client: TestClient, row: RoundTrip) -> None:
    assert _put(client, niche="x" * 161).status_code == 422


def test_nothing_sent_is_not_a_save(client: TestClient, row: RoundTrip) -> None:
    assert _put(client).status_code == 422


def test_a_save_that_did_not_stick_is_a_failed_save(client: TestClient, row: RoundTrip) -> None:
    row.drop_on_save = {"niche"}

    res = _put(client, niche="Public finance")

    assert res.status_code == 503
    assert res.json()["detail"]["message"].endswith("Your details are unchanged.")


def test_an_unreadable_profile_is_a_503_not_blank_fields(
    client: TestClient, row: RoundTrip
) -> None:
    row.fail_load = True

    res = client.get("/profile/details", headers=auth(make_token()))

    assert res.status_code == 503
