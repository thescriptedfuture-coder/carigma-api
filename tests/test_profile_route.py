"""GET /profile.

Fixtures drawn from the 12 live rows, not invented. The two that matter:

- A **fully onboarded** profile (10 of 12 look roughly like this).
- An **empty** one. Two live accounts signed up and never onboarded, so
  `{}` is a real state a real person is in, not a theoretical edge case.

`platforms` is NULL on all twelve, so the `["linkedin"]` default in
`db_to_profile` fires for every single user. That is not an edge case either —
it is the only thing standing between everyone and an empty platform list.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from carigma_api.routes import profile as routes
from carigma_api.services.repository import db_to_profile
from tests.conftest import auth, make_token

#: Column names, because that is what the repository reads from.
ONBOARDED_ROW: dict[str, Any] = {
    "user_id": "11111111-1111-1111-1111-111111111111",
    "name": "Ravi Kumar",
    "current_role": "Business Analyst",
    "target_roles": "Data Analyst, Product Analyst",
    "linkedin_headline": "Business Analyst | 5 years | SQL, Power BI",
    "skills": "SQL, Power BI, dbt",
    "location": "New Delhi",
    "education": "B.Tech",
    "onboarded": True,
    "plan": "free",
    "cv_template": "classic",
    "resume_retention_opt_in": True,
    "last_seen_jobs_at": "2026-08-14T09:00:00Z",
    # NULL on all twelve live rows.
    "platforms": None,
}


class FakeProfiles:
    def __init__(self, row: dict[str, Any] | None = None) -> None:
        self.row = row
        self.fail = False

    def load(self, user_id: str) -> dict[str, Any]:
        if self.fail:
            raise RuntimeError("db down")
        return db_to_profile(dict(self.row)) if self.row else {}


@pytest.fixture
def wired(client: TestClient, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    profiles = FakeProfiles(ONBOARDED_ROW)
    monkeypatch.setattr(routes, "_profiles", lambda request, settings: profiles)
    return client, profiles


def test_the_profile_needs_a_verified_token(client: TestClient) -> None:
    assert client.get("/profile").status_code == 401


def test_the_profile_comes_back_in_the_shape_the_app_reads(wired) -> None:  # type: ignore[no-untyped-def]
    """camelCase, from `db_to_profile`. Reading the snake_case side of this
    rename is what told users their headline was empty."""
    client, _profiles = wired

    body = client.get("/profile", headers=auth(make_token())).json()

    assert body["name"] == "Ravi Kumar"
    assert body["linkedinHeadline"].startswith("Business Analyst")
    assert body["currentRole"] == "Business Analyst"
    assert "linkedin_headline" not in body, "the column name leaked into the payload"


def test_a_null_platforms_column_becomes_linkedin(wired) -> None:  # type: ignore[no-untyped-def]
    """All twelve live rows have NULL here, so this default is the ONLY reason
    anyone has a platform at all. It is load-bearing for 100% of users, which
    makes it worth a test rather than a comment."""
    client, _profiles = wired

    body = client.get("/profile", headers=auth(make_token())).json()

    assert body["platforms"] == ["linkedin"]


def test_an_empty_profile_is_a_real_state_not_a_404(wired) -> None:  # type: ignore[no-untyped-def]
    """Two live accounts are in it. A 404 would tell a real user their profile
    does not exist, when the truth is they have not filled it in."""
    client, profiles = wired
    profiles.row = None

    res = client.get("/profile", headers=auth(make_token()))

    assert res.status_code == 200
    assert res.json()["onboarded"] is False


def test_a_failed_read_is_not_reported_as_an_empty_profile(wired) -> None:  # type: ignore[no-untyped-def]
    """ "We could not look" must not render as "you have not started" — the
    second sends someone back through onboarding they already completed."""
    client, profiles = wired
    profiles.fail = True

    res = client.get("/profile", headers=auth(make_token()))

    assert res.status_code == 503
    assert res.json()["detail"]["error"] == "read_failed"


def test_onboarded_is_carried_not_inferred_from_an_empty_dict(wired) -> None:  # type: ignore[no-untyped-def]
    client, _profiles = wired

    assert client.get("/profile", headers=auth(make_token())).json()["onboarded"] is True


def test_the_last_jobs_visit_is_readable(wired) -> None:  # type: ignore[no-untyped-def]
    """`last_seen_jobs_at` is filled on 8 of 12 live rows and NOTHING mapped
    it, so "new since you last looked" had no source. Added to the mapping."""
    client, _profiles = wired

    assert client.get("/profile", headers=auth(make_token())).json()["lastSeenJobsAt"]


def test_reading_a_profile_never_writes_one() -> None:
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

    for forbidden in ("save", "upsert", "insert", "update", "credits", "apply_delta"):
        assert forbidden not in referenced, f"{forbidden!r} appears on a read-only path"


def test_the_fake_can_produce_both_states() -> None:
    """Assert the precondition: a fake that could only return a full profile
    would make the empty-profile test unreachable."""
    assert FakeProfiles(None).load("u") == {}
    assert FakeProfiles(ONBOARDED_ROW).load("u")["name"] == "Ravi Kumar"


# ── The mapping refuses what it does not know ──────────────────────────────


def test_an_unknown_profile_field_is_refused_not_dropped() -> None:
    """The silent filter is how a save became a no-op that reported success.

    `profiles.save(user_id, {"resume_retention_opt_in": False})` passed a
    COLUMN name to a mapping keyed on JSON names. The old `if k in
    _PROFILE_COLUMNS` filter discarded the whole payload and returned happily,
    so a user turning resume retention off had their files deleted and their
    stated preference thrown away.
    """
    from carigma_api.services.repository import UnknownProfileField, profile_to_db

    with pytest.raises(UnknownProfileField, match="resumeRetentionOptIn"):
        profile_to_db({"resume_retention_opt_in": False})


def test_the_refusal_names_the_field_the_caller_meant() -> None:
    """An error saying "unknown key" sends someone reading the mapping. One
    saying "did you mean `linkedinHeadline`?" ends it."""
    from carigma_api.services.repository import UnknownProfileField, profile_to_db

    with pytest.raises(UnknownProfileField, match="linkedinHeadline"):
        profile_to_db({"linkedin_headline": "x"})


def test_every_known_field_still_round_trips() -> None:
    """Assert the precondition. A `profile_to_db` that raised on everything
    would pass both tests above and break the entire app."""
    from carigma_api.services.repository import _PROFILE_COLUMNS, profile_to_db

    columns = profile_to_db(dict.fromkeys(_PROFILE_COLUMNS, "x"))

    assert set(columns) == set(_PROFILE_COLUMNS.values())


def test_the_retention_flag_reaches_a_real_column() -> None:
    """End to end through the mapping: the key Settings writes must translate."""
    from carigma_api.services.repository import profile_to_db

    assert profile_to_db({"resumeRetentionOptIn": False}) == {"resume_retention_opt_in": False}
