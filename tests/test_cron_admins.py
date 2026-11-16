"""An admin is not a job seeker, in the crons too (16 Nov).

Every cron ran with `--only ravisravi31@gmail.com` from September until the
switch: the email log shows exactly one address per run. Taking it off makes
`EMAIL_AUDIENCE=everyone` and `SUNDAY_AUDIENCE=everyone` mean what they say,
and would also have mailed support@ a brief and a Sunday review and proposed
it a week of posts: it finished setup during UAT, so it has an onboarded
profile like anybody else. These are the cases that keep it out.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any

import pytest

import carigma_api.services.sunday as sunday_module
from carigma_api.services import emails as mail
from carigma_api.services.sunday import SundayReport
from tests.fake_supabase import FakeDB

_SPEC = importlib.util.spec_from_file_location(
    "send_emails", Path(__file__).resolve().parents[1] / "scripts" / "send_emails.py"
)
assert _SPEC and _SPEC.loader
send_emails = importlib.util.module_from_spec(_SPEC)
sys.modules["send_emails"] = send_emails
_SPEC.loader.exec_module(send_emails)

EMAILS = {"u-ravi": "ravi@example.com", "u-admin": "Support@Example.com", "u-v1": "v1@example.com"}


@pytest.fixture
def db(monkeypatch: pytest.MonkeyPatch) -> FakeDB:
    fake = FakeDB()
    for uid in EMAILS:
        fake.seed("profiles", {"user_id": uid, "name": uid, "onboarded": True})
    monkeypatch.setattr(send_emails, "emails_by_user_id", lambda _db: dict(EMAILS))
    return fake


def test_the_brief_and_the_review_leave_the_admin_out(db: FakeDB) -> None:
    got = send_emails._recipients(db, admins=frozenset({"support@example.com"}))

    assert sorted(r["email"] for r in got) == ["ravi@example.com", "v1@example.com"]


def test_without_admins_everybody_with_a_profile_is_a_recipient(db: FakeDB) -> None:
    """The control: the case above passes because of the admin list, not
    because the fixture lost a row."""
    assert len(send_emails._recipients(db)) == 3


def test_a_cron_without_admin_emails_says_so(caplog: pytest.LogCaptureFixture) -> None:
    class _S:
        admin_email_list: list[str] = []

    with caplog.at_level(logging.WARNING, logger="send_emails"):
        assert send_emails._admins(_S()) == frozenset()

    assert "ADMIN_EMAILS is not set" in caplog.text


def test_the_admin_list_is_read_lower_cased() -> None:
    class _S:
        admin_email_list = ["Support@Example.com"]

    assert send_emails._admins(_S()) == frozenset({"support@example.com"})


def test_the_weekly_cron_hands_the_sunday_pass_the_admins_ids(
    db: FakeDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The entry point the cron actually runs, with no `--only`."""
    seen: dict[str, Any] = {}

    class _S:
        admin_email_list = ["support@example.com"]
        sunday_audience = "everyone"

    def fake_sunday(_db: Any, **kw: Any) -> SundayReport:
        seen.update(kw)
        return SundayReport()

    monkeypatch.setattr(sys, "argv", ["send_emails.py", "weekly"])
    monkeypatch.setattr(send_emails, "Settings", _S)
    monkeypatch.setattr(send_emails.deploy, "wrong_branch", lambda _s: None)
    monkeypatch.setattr(send_emails, "service_client", lambda _s: db)
    monkeypatch.setattr(sunday_module, "sunday", fake_sunday)
    monkeypatch.setattr(send_emails, "run", lambda *a, **k: mail.RunSummary())

    assert send_emails.main() == 0
    assert seen["who"] == "everyone"
    assert seen["exclude"] == frozenset({"u-admin"})
    assert seen["only"] is None
