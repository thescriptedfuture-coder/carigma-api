"""Settings over HTTP.

Two rules are checked at the boundary rather than only in the service layer,
because these are the ones a future endpoint could quietly violate:

- **No jobs endpoint takes a platform parameter.** Platform preference governs
  profile optimization only; there is no compliant access to Naukri listings.
- **The reset endpoint cannot be used to enumerate accounts.**
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from carigma_api.routes import settings as settings_routes
from carigma_api.services.repository import profile_to_db
from tests.conftest import auth, make_token


class FakeProfiles:
    def __init__(self, profile: dict[str, Any] | None = None, fail: bool = False):
        self.profile = profile if profile is not None else {"platforms": ["linkedin"]}
        self.saved: dict[str, Any] = {}
        #: What would actually land in the table.
        self.columns: dict[str, Any] = {}
        self.fail = fail

    def load(self, user_id: str) -> dict[str, Any]:
        return self.profile

    def save(self, user_id: str, profile: dict[str, Any]) -> dict[str, Any]:
        """Goes through the REAL mapping, deliberately.

        The previous version recorded whatever it was handed, so a test could
        assert a value reached `save()` while the real `profile_to_db` dropped
        it on the floor. That is exactly what happened to the resume-retention
        flag: the test passed, the column never changed. A fake that is easier
        than reality tests nothing.
        """
        if self.fail:
            raise RuntimeError("db down")
        self.columns.update(profile_to_db(profile))
        self.saved.update(profile)
        return self.saved


class FakeResumeStore:
    def __init__(self, files: list[str] | None = None):
        self.files = files or []
        self.deleted: list[str] = []

    def list_files(self, user_id: str) -> list[str]:
        return list(self.files)

    def delete(self, user_id: str, path: str) -> bool:
        self.deleted.append(path)
        self.files = [f for f in self.files if f != path]
        return True


@pytest.fixture
def profiles(monkeypatch: pytest.MonkeyPatch) -> FakeProfiles:
    fake = FakeProfiles()
    store = FakeResumeStore()
    monkeypatch.setattr(settings_routes, "_profiles", lambda request, settings: (fake, None))
    monkeypatch.setattr(settings_routes, "SupabaseResumeStore", lambda client: store)
    fake.store = store  # type: ignore[attr-defined]
    return fake


# ── Platform preference ────────────────────────────────────────────────────


def test_changing_platforms_requires_a_verified_token(client: TestClient) -> None:
    res = client.put("/profile/platforms", json={"platforms": ["linkedin"]})
    assert res.status_code == 401


def test_adding_a_platform_persists_it(client: TestClient, profiles: FakeProfiles) -> None:
    res = client.put(
        "/profile/platforms",
        json={"platforms": ["linkedin", "naukri"]},
        headers=auth(make_token()),
    )

    assert res.status_code == 200
    assert res.json()["tracks_added"] == ["naukri"]
    assert profiles.saved["platforms"] == ["linkedin", "naukri"]


def test_removing_a_platform_warns_that_history_is_kept(
    client: TestClient, profiles: FakeProfiles
) -> None:
    profiles.profile = {"platforms": ["linkedin", "naukri"]}
    res = client.put(
        "/profile/platforms", json={"platforms": ["linkedin"]}, headers=auth(make_token())
    )

    body = res.json()
    assert body["tracks_removed"] == ["naukri"]
    assert "kept, not deleted" in body["notice"]


def test_the_last_platform_cannot_be_removed(client: TestClient, profiles: FakeProfiles) -> None:
    res = client.put("/profile/platforms", json={"platforms": []}, headers=auth(make_token()))

    assert res.status_code == 400
    assert res.json()["detail"]["error"] == "no_platform"
    assert profiles.saved == {}, "nothing should have been written"


def test_an_unknown_platform_is_rejected_by_the_schema(
    client: TestClient, profiles: FakeProfiles
) -> None:
    res = client.put(
        "/profile/platforms", json={"platforms": ["indeed"]}, headers=auth(make_token())
    )
    assert res.status_code == 422


def test_a_save_failure_says_settings_are_unchanged(client: TestClient, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    fake = FakeProfiles(fail=True)
    monkeypatch.setattr(settings_routes, "_profiles", lambda request, settings: (fake, None))

    res = client.put(
        "/profile/platforms", json={"platforms": ["naukri"]}, headers=auth(make_token())
    )

    assert res.status_code == 503
    assert "unchanged" in res.json()["detail"]["message"]
    assert "Traceback" not in str(res.json())


def test_no_jobs_endpoint_accepts_a_platform_parameter(client: TestClient) -> None:
    """Roadmap 7.1, restated in every brief since: platform preference governs
    profile optimization ONLY. There is no compliant access to Naukri listings,
    so a platform-filtered jobs endpoint could never be honoured.
    """
    schema = client.app.openapi()  # type: ignore[attr-defined]
    jobs_paths = [p for p in schema["paths"] if p.startswith("/jobs")]

    for path in jobs_paths:
        for method in schema["paths"][path].values():
            names = [p["name"] for p in method.get("parameters", [])]
            assert "platform" not in names, f"{path} takes a platform parameter"
            assert "platforms" not in names, f"{path} takes a platforms parameter"


# ── Resume retention ───────────────────────────────────────────────────────


def test_opting_out_deletes_and_returns_a_real_count(
    client: TestClient, profiles: FakeProfiles
) -> None:
    profiles.store.files = ["u/resume.pdf"]  # type: ignore[attr-defined]

    res = client.put(
        "/profile/resume-retention", json={"opt_in": False}, headers=auth(make_token())
    )

    body = res.json()
    assert body["resumeRetentionOptIn"] is False
    assert body["deletion"]["deleted"] == 1
    assert body["deletion"]["complete"] is True
    # Asserted on the COLUMNS, not on the argument. The argument reaching
    # `save()` proves nothing about what the database received.
    assert profiles.columns["resume_retention_opt_in"] is False


def test_opting_out_with_nothing_stored_says_so_honestly(
    client: TestClient, profiles: FakeProfiles
) -> None:
    res = client.put(
        "/profile/resume-retention", json={"opt_in": False}, headers=auth(make_token())
    )

    body = res.json()
    assert body["deletion"]["deleted"] == 0
    assert "Nothing was stored" in body["deletion"]["message"]


def test_opting_in_deletes_nothing(client: TestClient, profiles: FakeProfiles) -> None:
    profiles.store.files = ["u/resume.pdf"]  # type: ignore[attr-defined]

    res = client.put("/profile/resume-retention", json={"opt_in": True}, headers=auth(make_token()))

    assert res.json()["resumeRetentionOptIn"] is True
    assert "deletion" not in res.json()
    assert profiles.store.deleted == []  # type: ignore[attr-defined]


def test_the_retention_setting_explains_what_it_does(
    client: TestClient, profiles: FakeProfiles
) -> None:
    off = client.put(
        "/profile/resume-retention", json={"opt_in": False}, headers=auth(make_token())
    ).json()
    assert "discarded" in off["explainer"]


def test_retention_requires_a_verified_token(client: TestClient) -> None:
    assert client.put("/profile/resume-retention", json={"opt_in": False}).status_code == 401


# ── Password reset ─────────────────────────────────────────────────────────


def test_the_reset_endpoint_is_unauthenticated(client: TestClient) -> None:
    """Someone who cannot log in cannot present a token."""
    res = client.post("/auth/reset-password", json={"email": "a@b.com"})
    assert res.status_code == 200


def test_the_reset_reply_is_identical_for_any_address(client: TestClient) -> None:
    """Otherwise the form becomes an account-enumeration oracle."""
    one = client.post("/auth/reset-password", json={"email": "real@example.com"}).json()
    two = client.post("/auth/reset-password", json={"email": "nobody@example.com"}).json()

    assert one["title"] == two["title"]
    assert one["resend_after_seconds"] == two["resend_after_seconds"]
    assert one["persistent"] == two["persistent"] is True
    # The only difference is the address echoed back, and both are conditional.
    assert one["body"].replace("real@example.com", "X") == two["body"].replace(
        "nobody@example.com", "X"
    )


def test_the_confirmation_carries_a_resend_cooldown(client: TestClient) -> None:
    body = client.post("/auth/reset-password", json={"email": "a@b.com"}).json()
    assert body["resend_after_seconds"] == 60


def test_obvious_junk_is_rejected_before_reaching_the_mailer(client: TestClient) -> None:
    assert client.post("/auth/reset-password", json={"email": "not-an-email"}).status_code == 422


# ── Email preferences ──────────────────────────────────────────────────────


def test_reading_email_preferences_requires_a_verified_token(client: TestClient) -> None:
    assert client.get("/profile/email-preferences").status_code == 401


def test_changing_email_preferences_requires_a_verified_token(client: TestClient) -> None:
    res = client.put(
        "/profile/email-preferences",
        json={"daily_brief": False, "weekly_review": True, "unsubscribed_all": False},
    )
    assert res.status_code == 401


def test_an_account_that_never_touched_this_reads_as_opted_in(
    client: TestClient, profiles: FakeProfiles
) -> None:
    profiles.profile = {"platforms": ["linkedin"]}

    body = client.get("/profile/email-preferences", headers=auth(make_token())).json()

    assert body["daily_brief"] is True
    assert body["effective"] == {"daily_brief": True, "weekly_review": True}


def test_turning_the_daily_brief_off_persists_it(
    client: TestClient, profiles: FakeProfiles
) -> None:
    res = client.put(
        "/profile/email-preferences",
        json={"daily_brief": False, "weekly_review": True, "unsubscribed_all": False},
        headers=auth(make_token()),
    )

    assert res.status_code == 200
    assert res.json()["effective"] == {"daily_brief": False, "weekly_review": True}
    assert profiles.saved["preferences"]["email"]["daily_brief"] is False


def test_the_write_merges_rather_than_replacing_the_preferences_column(
    client: TestClient, profiles: FakeProfiles
) -> None:
    """The endpoint-level version of the service test. `save()` writes the
    column wholesale, so a replace here would erase the agent's user memory."""
    profiles.profile = {"platforms": ["linkedin"], "preferences": {"memory": ["no crypto"]}}

    client.put(
        "/profile/email-preferences",
        json={"daily_brief": False, "weekly_review": False, "unsubscribed_all": True},
        headers=auth(make_token()),
    )

    assert profiles.saved["preferences"]["memory"] == ["no crypto"]


def test_a_failed_save_says_the_settings_are_unchanged(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Assuming you are unsubscribed when you are not is how a beta user
    becomes a spam report."""
    fake = FakeProfiles(fail=True)
    monkeypatch.setattr(settings_routes, "_profiles", lambda request, settings: (fake, None))

    res = client.put(
        "/profile/email-preferences",
        json={"daily_brief": False, "weekly_review": False, "unsubscribed_all": True},
        headers=auth(make_token()),
    )

    assert res.status_code == 503
    detail = res.json()["detail"]
    assert "unchanged" in detail["message"]
    assert "support@carigma.in" in detail["message"]
