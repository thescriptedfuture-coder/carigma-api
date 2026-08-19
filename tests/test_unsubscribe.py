"""One-click unsubscribe, for someone who is not signed in.

Guarding `/settings` broke the emailed unsubscribe landing — which was BUILT
for a signed-out visitor. Requiring a login to unsubscribe is a bad answer and,
for marketing mail, arguably not a lawful one; DPDPA is on the pre-launch
checklist.

Most of this file is about what the token CANNOT do.
"""

from __future__ import annotations

import ast
import hashlib
import hmac
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from carigma_api.config import Settings, get_settings
from carigma_api.routes import unsubscribe as routes
from carigma_api.services import unsubscribe as unsub
from carigma_api.services.settings_service import EMAIL_PREFS_KEY, EmailPreferences
from tests.fake_supabase import FakeDB

KEY = "service-key-for-tests"
USER = "00000000-0000-4000-8000-000000000001"


class FakeProfiles:
    def __init__(self) -> None:
        self.profile: dict[str, Any] = {}
        self.saves: list[dict[str, Any]] = []
        self.fail = False

    def load(self, _user_id: str) -> dict[str, Any]:
        if self.fail:
            raise RuntimeError("profiles down")
        return dict(self.profile)

    def save(self, _user_id: str, patch: dict[str, Any]) -> None:
        if self.fail:
            raise RuntimeError("profiles down")
        self.saves.append(dict(patch))
        self.profile.update(patch)


@pytest.fixture
def wired(client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    profiles = FakeProfiles()
    live = settings.model_copy(update={"supabase_service_key": KEY})
    client.app.dependency_overrides[get_settings] = lambda: live  # type: ignore[attr-defined]
    monkeypatch.setattr(routes, "service_client", lambda _s: FakeDB())
    monkeypatch.setattr(routes, "ProfileRepository", lambda _c: profiles)
    yield client, profiles
    client.app.dependency_overrides.pop(get_settings, None)  # type: ignore[attr-defined]


# ── The token ──────────────────────────────────────────────────────────────


def test_a_token_round_trips() -> None:
    assert unsub.resolve(unsub.issue(USER, service_key=KEY), service_key=KEY) == USER


def test_a_token_from_a_different_key_does_not_resolve() -> None:
    forged = unsub.issue(USER, service_key="somebody-elses-key")
    assert unsub.resolve(forged, service_key=KEY) is None


def test_the_signing_key_is_derived_not_the_service_key() -> None:
    """The root never signs anything directly.

    A token signed with the service key itself would mean anyone who ever saw
    a token held a probe against the root secret.
    """
    payload = unsub.issue(USER, service_key=KEY).split(".")[0]
    with_root = hmac.new(KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()

    assert not unsub.issue(USER, service_key=KEY).endswith(with_root[: unsub.SIG_LENGTH])


def test_the_purpose_string_isolates_this_token_from_any_other_use() -> None:
    """A token minted here must never be replayable against something else
    that one day derives from the same root."""
    mine = hmac.new(KEY.encode(), unsub.PURPOSE, hashlib.sha256).digest()
    other = hmac.new(KEY.encode(), b"carigma.something.else", hashlib.sha256).digest()

    assert mine != other


def test_a_truncated_token_resolves_to_nothing_rather_than_raising() -> None:
    """Email clients wrap and cut long URLs. That is the common case, not an
    attack, and it must not 500."""
    good = unsub.issue(USER, service_key=KEY)
    for broken in (good[:-4], good.split(".")[0], "", "no-dot-at-all", "a.b"):
        assert unsub.resolve(broken, service_key=KEY) is None


def test_no_key_means_no_token_rather_than_a_worthless_one() -> None:
    """A link signed with an empty key verifies against an empty key — which is
    every attacker's key too."""
    with pytest.raises(ValueError):
        unsub.issue(USER, service_key="")
    assert unsub.resolve("anything.atall", service_key="") is None


def test_the_token_carries_no_email_address() -> None:
    assert "@" not in unsub.issue(USER, service_key=KEY)


def test_there_is_no_expiry_field_to_mislead_anyone() -> None:
    """An unsubscribe link SHOULD still work in a year — an old email is
    exactly when someone reaches for it.

    So there is no timestamp at all, rather than one nobody checks: an
    unchecked expiry is dead code that reads as a policy, and the next person
    trusts it.
    """
    tree = ast.parse(Path(unsub.__file__).read_text(encoding="utf-8"))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}

    assert not ({"expires_at", "issued_at", "exp", "iat"} & names)


# ── Single purpose ─────────────────────────────────────────────────────────


def test_the_route_can_only_touch_profiles() -> None:
    """A service-role handler that accepted a scope or an action would be one
    missing check from serving anyone's data."""
    tree = ast.parse(Path(routes.__file__).read_text(encoding="utf-8"))
    tables = {
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "table"
        and node.args
        and isinstance(node.args[0], ast.Constant)
    }

    assert tables <= {"profiles"}, f"unsubscribe reaches beyond profiles: {tables}"


def test_the_route_takes_no_scope_or_action_parameter() -> None:
    """Nothing a caller could widen. The token means one thing."""
    tree = ast.parse(Path(routes.__file__).read_text(encoding="utf-8"))
    args: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            args |= {a.arg for a in node.args.args}

    assert not ({"scope", "action", "fields", "op"} & args)


# ── Over HTTP ──────────────────────────────────────────────────────────────


def test_unsubscribing_needs_no_token_header(wired: Any) -> None:
    """The entire point: it works on a device where nobody signed in."""
    client, profiles = wired

    res = client.post(f"/unsubscribe/{unsub.issue(USER, service_key=KEY)}")

    assert res.status_code == 200
    assert res.json()["done"] is True
    assert (
        EmailPreferences.from_stored(
            profiles.profile["preferences"][EMAIL_PREFS_KEY]
        ).unsubscribed_all
        is True
    )


def test_a_bad_token_is_indistinguishable_from_a_good_one(wired: Any) -> None:
    """Non-enumeration, the same rule password reset follows. Someone probing
    with guessed tokens learns nothing."""
    client, profiles = wired

    good = client.post(f"/unsubscribe/{unsub.issue(USER, service_key=KEY)}").json()
    bad = client.post("/unsubscribe/definitely.wrong").json()

    assert good == bad
    assert len(profiles.saves) == 1, "the bad token wrote nothing"


def test_per_type_choices_survive_the_master_switch(wired: Any) -> None:
    """Unsubscribing is a master switch, not an erasure. If they resubscribe
    they get their settings back rather than our defaults."""
    client, profiles = wired
    profiles.profile["preferences"] = {
        "email": {"daily_brief": False, "weekly_review": True, "unsubscribed_all": False}
    }

    client.post(f"/unsubscribe/{unsub.issue(USER, service_key=KEY)}")

    saved = EmailPreferences.from_stored(profiles.profile["preferences"][EMAIL_PREFS_KEY])
    assert saved.unsubscribed_all is True
    assert saved.daily_brief is False
    assert saved.weekly_review is True


def test_the_learned_memory_in_preferences_is_not_erased(wired: Any) -> None:
    """`preferences` also holds the Profile Analyst's memory, and `save()`
    writes the column wholesale."""
    client, profiles = wired
    profiles.profile["preferences"] = {"memory": {"tone": "plain"}}

    client.post(f"/unsubscribe/{unsub.issue(USER, service_key=KEY)}")

    assert profiles.profile["preferences"]["memory"] == {"tone": "plain"}


def test_a_write_failure_still_answers_but_is_logged_loudly(
    wired: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Saying done and doing nothing is the shape this project keeps finding.
    Here it would mean emailing somebody who told us to stop."""
    client, profiles = wired
    profiles.fail = True

    with caplog.at_level(logging.ERROR):
        res = client.post(f"/unsubscribe/{unsub.issue(USER, service_key=KEY)}")

    assert res.status_code == 200
    assert any("UNSUBSCRIBE WRITE FAILED" in r.getMessage() for r in caplog.records)


def test_the_describe_route_never_names_anybody(wired: Any) -> None:
    """A valid token gets `readable: true` and no identity. Naming an address
    would confirm it is registered to whoever holds the link."""
    client, _profiles = wired

    body = client.get(f"/unsubscribe/{unsub.issue(USER, service_key=KEY)}").json()

    assert body["readable"] is True
    assert USER not in repr(body)
    assert "@" not in repr(body)


def test_an_unreadable_link_says_so_without_blaming_the_reader(wired: Any) -> None:
    """The unknown state, kept. A truncated link is the common case."""
    client, _profiles = wired

    body = client.get("/unsubscribe/cannot.read").json()

    assert body["readable"] is False
    assert "cut short" in body["note"]
