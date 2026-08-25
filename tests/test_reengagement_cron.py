"""The lapsed-digest cron.

Every test here is a refusal, because that is what this job mostly does. The
one that matters most: **no live market batch means nothing sends at all.** A
stale digest resent is the manufactured contact the whole feature exists to
refuse, and the lapsed inbox is where it costs the channel permanently.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from carigma_api.services import emails as mail
from carigma_api.services.reengagement import TOTAL_SENDS, State

# The cron is a script, not a package module.
_SPEC = importlib.util.spec_from_file_location(
    "send_emails", Path(__file__).resolve().parents[1] / "scripts" / "send_emails.py"
)
assert _SPEC and _SPEC.loader
send_emails = importlib.util.module_from_spec(_SPEC)
sys.modules["send_emails"] = send_emails
_SPEC.loader.exec_module(send_emails)

NOW = datetime.now(UTC)
LAPSED = NOW - timedelta(days=30)

LIVE_BATCH = {
    "batch_key": "mkt_w33",
    "items": [
        {
            "headline": "Analytics hiring up 12%",
            "detail": "Bengaluru and Pune.",
            "source_name": "NASSCOM",
            "source_url": "https://example.com/r",
        }
    ],
    "published_at": (NOW - timedelta(days=1)).isoformat(),
    "next_refresh": None,
}

STALE_BATCH = {**LIVE_BATCH, "published_at": (NOW - timedelta(days=90)).isoformat()}
DRAFT_BATCH = {**LIVE_BATCH, "published_at": None}


class FakeMailer:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    def send(self, email: Any) -> None:
        self.sent.append(email)

    def close(self) -> None:
        pass


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A cron run with every external edge faked, and nothing else stubbed."""
    state: dict[str, Any] = {
        "batches": [dict(LIVE_BATCH)],
        "sequences": [
            {
                "id": 1,
                "user_id": "u1",
                "sends_made": 0,
                "state": "active",
                "next_due_at": (NOW - timedelta(hours=1)).isoformat(),
                "last_sent_at": None,
                "closed_at": None,
            }
        ],
        "updates": [],
        "claimed": set(),
        "prefs": mail.Preferences(),
        "sign_in": {"u1": LAPSED},
    }
    mailer = FakeMailer()
    state["mailer"] = mailer

    # SMTP must LOOK configured or the runner forces --dry-run and every
    # "it sent" assertion below passes for the wrong reason — it would be
    # asserting that nothing happened, in a test whose name says it did.
    # The tests are hermetic (conftest blanks .env), so this is not inherited.
    class _S:
        smtp_host = "smtp.example.com"
        smtp_user = "bot@example.com"
        smtp_pass = "x"
        smtp_port = 465
        smtp_from = "Carigma <bot@example.com>"
        supabase_url = "https://example.supabase.co"
        supabase_service_key = "sb_secret_test"
        # These tests are about the sequence logic, so the deploy-level audience
        # gate is opened explicitly. Saying so beats a default: the gate admits
        # nobody unless someone decides otherwise, and that is the point of it.
        email_audience = "everyone"
        email_allowlist = ""
        # The lapsed digest is marketing mail, so it now carries an unsubscribe
        # footer — and is REFUSED without the pieces to build one.
        app_url = "https://app.example.com"

    monkeypatch.setattr(send_emails, "Settings", _S)
    monkeypatch.setattr(send_emails, "service_client", lambda settings: object())
    monkeypatch.setattr(send_emails, "SmtpMailer", lambda settings: mailer)
    monkeypatch.setattr(send_emails, "emails_by_user_id", lambda db: {"u1": "u1@example.com"})
    monkeypatch.setattr(send_emails, "_last_sign_in", lambda db: state["sign_in"])
    monkeypatch.setattr(send_emails, "_prefs_for_user", lambda db, uid: state["prefs"])
    monkeypatch.setattr(
        send_emails,
        "_rows",
        lambda db, table: state["batches"] if table == "market_digests" else state["sequences"],
    )
    monkeypatch.setattr(
        send_emails,
        "_update_sequence",
        lambda db, sid, update: state["updates"].append((sid, update)),
    )

    class Log:
        def record(self, *a: Any, **k: Any) -> None:
            pass

        def claim_period(self, user_id: str, email_type: str, period_key: str) -> bool:
            key = (user_id, email_type, period_key)
            if key in state["claimed"]:
                return False
            state["claimed"].add(key)
            return True

    monkeypatch.setattr(send_emails, "SupabaseSendLog", lambda db: Log())
    return state


def run(**kw: Any) -> Any:
    return send_emails.run_reengagement(dry_run=False, **kw)


def test_the_fixture_actually_permits_a_send(env: dict[str, Any]) -> None:
    """The precondition every "it sent" test below depends on.

    Without configured SMTP the runner forces --dry-run, and every send
    assertion would pass by asserting nothing happened. Proven live here rather
    than assumed in each test.
    """
    run()

    assert env["mailer"].sent, "the fixture must permit a real send, or the suite proves nothing"


# ── The refusal that matters most ──────────────────────────────────────────


def test_no_live_batch_sends_nothing_to_anyone(env: dict[str, Any]) -> None:
    """Asked once, for the whole run. The answer cannot differ per user, and a
    per-user check would invite a per-user fallback."""
    env["batches"] = [dict(DRAFT_BATCH)]

    run()

    assert env["mailer"].sent == []
    assert env["updates"] == [], "a sequence must not advance on a send that never happened"


def test_an_expired_batch_is_not_resent(env: dict[str, Any]) -> None:
    env["batches"] = [dict(STALE_BATCH)]

    run()

    assert env["mailer"].sent == []


def test_no_batches_at_all_sends_nothing(env: dict[str, Any]) -> None:
    env["batches"] = []

    run()

    assert env["mailer"].sent == []


# ── The happy path ─────────────────────────────────────────────────────────


def test_a_due_lapsed_user_gets_the_market_digest(env: dict[str, Any]) -> None:
    run()

    assert len(env["mailer"].sent) == 1
    sent = env["mailer"].sent[0]
    assert sent.to == "u1@example.com"
    assert "Analytics hiring up 12%" in sent.body
    assert sent.email_type is mail.EmailType.MARKET_DIGEST


def test_the_digest_is_marketing_so_an_unsubscribe_silences_it() -> None:
    """If it were not marketing, `Preferences.allows` would let it through to
    someone who unsubscribed from everything."""
    assert mail.EmailType.MARKET_DIGEST.is_marketing is True
    assert mail.Preferences(unsubscribed_all=True).allows(mail.EmailType.MARKET_DIGEST) is False


def test_turning_off_the_weekly_review_also_stops_the_market_digest(
    env: dict[str, Any],
) -> None:
    """Someone who said no to a weekly email has already answered. Lapsing is
    not consent."""
    env["prefs"] = mail.Preferences(weekly_review=False)

    run()

    assert env["mailer"].sent == []


def test_a_send_advances_the_sequence(env: dict[str, Any]) -> None:
    run()

    assert len(env["updates"]) == 1
    _, update = env["updates"][0]
    assert update["sends_made"] == 1
    assert update["state"] == str(State.ACTIVE)


def test_the_eighth_send_closes_the_sequence_as_finished(env: dict[str, Any]) -> None:
    env["sequences"][0]["sends_made"] = TOTAL_SENDS - 1

    run()

    _, update = env["updates"][0]
    assert update["state"] == str(State.FINISHED)
    assert update["next_due_at"] is None


# ── Everything that must NOT consume a send ────────────────────────────────


def test_an_unsubscribed_user_does_not_burn_one_of_the_eight(env: dict[str, Any]) -> None:
    """Otherwise someone who was opted out the whole time is silently
    "finished" having received nothing."""
    env["prefs"] = mail.Preferences(unsubscribed_all=True)

    run()

    assert env["updates"] == []


def test_a_duplicate_claim_does_not_advance_the_sequence(env: dict[str, Any]) -> None:
    run()
    env["updates"].clear()
    env["sequences"][0]["next_due_at"] = (NOW - timedelta(hours=1)).isoformat()

    run()  # same period key — digest_log refuses the claim

    assert env["updates"] == []


def test_a_sequence_that_is_not_due_is_left_alone(env: dict[str, Any]) -> None:
    env["sequences"][0]["next_due_at"] = (NOW + timedelta(days=3)).isoformat()

    run()

    assert env["mailer"].sent == []
    assert env["updates"] == []


def test_a_finished_sequence_is_never_picked_up(env: dict[str, Any]) -> None:
    env["sequences"][0]["state"] = "finished"
    env["sequences"][0]["closed_at"] = NOW.isoformat()

    run()

    assert env["mailer"].sent == []


# ── The reset ──────────────────────────────────────────────────────────────


def test_signing_in_closes_the_sequence_instead_of_sending(env: dict[str, Any]) -> None:
    env["sign_in"] = {"u1": NOW - timedelta(days=1)}

    run()

    assert env["mailer"].sent == []
    _, update = env["updates"][0]
    assert update["state"] == str(State.RETURNED)
    assert update["closed_at"]


def test_unreadable_sign_in_times_stop_sending_rather_than_guessing(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty map makes every open sequence look not-lapsed, so it closes
    them rather than mailing. Failing toward silence is the right direction:
    the alternative is mailing someone who came back."""
    monkeypatch.setattr(send_emails, "_last_sign_in", lambda db: {})

    run()

    assert env["mailer"].sent == []


def test_a_user_with_no_email_is_skipped_not_crashed(env: dict[str, Any], monkeypatch) -> None:
    monkeypatch.setattr(send_emails, "emails_by_user_id", lambda db: {})

    run()

    assert env["mailer"].sent == []
