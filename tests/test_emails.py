"""The email loop.

The central rule: **a daily brief with nothing in it is a lie by implication.**
It claims something needs you when nothing does, and it trains people to ignore
the emails that matter. Wednesday is quiet by design.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from carigma_api.services.emails import (
    Audience,
    DailyFacts,
    Email,
    EmailType,
    Preferences,
    RunSummary,
    SendOutcome,
    SendStatus,
    WeeklyFacts,
    build_daily_brief,
    build_weekly_review,
    daily_key,
    send,
    weekly_key,
)
from carigma_api.services.weekly import GuiltyCopy


class FakeMailer:
    def __init__(self, explode: bool = False):
        self.sent: list[Email] = []
        self.explode = explode

    def send(self, email: Email) -> None:
        if self.explode:
            raise RuntimeError("smtp down")
        self.sent.append(email)


class FakeLog:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []
        self.claimed: set[tuple[str, str, str]] = set()

    def record(
        self,
        email: Email | None,
        *,
        recipient: str,
        email_type: str,
        status: str,
        detail: str | None,
    ) -> None:
        self.records.append(
            {"recipient": recipient, "type": email_type, "status": status, "detail": detail}
        )

    def claim_period(self, user_id: str, email_type: str, period_key: str) -> bool:
        key = (user_id, email_type, period_key)
        if key in self.claimed:
            return False
        self.claimed.add(key)
        return True


def deliver(email: Email | None, **over: Any) -> tuple[SendOutcome, FakeMailer, FakeLog]:
    mailer = over.pop("mailer", None) or FakeMailer()
    log = over.pop("log", None) or FakeLog()
    outcome = send(
        email,
        mailer=mailer,
        log=log,
        user_id=over.pop("user_id", "u1"),
        recipient=over.pop("recipient", "a@b.com"),
        email_type=over.pop("email_type", EmailType.DAILY_BRIEF),
        period_key=over.pop("period_key", "2026-08-08"),
        prefs=over.pop("prefs", Preferences()),
        # Marketing mail without an unsubscribe link is refused before it is
        # sent, so every test that expects a send has to supply the pieces the
        # footer is built from. Overridable, because one test below removes
        # them deliberately.
        service_key=over.pop("service_key", "test-signing-key"),
        app_url=over.pop("app_url", "https://app.example.com"),
        # The deploy-level audience gate, opened explicitly.
        #
        # `send()` admits NOBODY when no audience is passed, which is the whole
        # design: a caller that forgets sends nothing rather than everything.
        # Saying `unrestricted()` here is the visible counterpart — these tests
        # are about send()'s own logic, not about who this deploy may write to,
        # and a default would hide the distinction they depend on.
        audience=over.pop("audience", Audience.unrestricted()),
        **over,
    )
    return outcome, mailer, log


# ── Skip when nothing happened ─────────────────────────────────────────────


def test_a_quiet_day_produces_no_email_at_all() -> None:
    """Wednesday is quiet by design. A manufactured email breaks the honesty
    pillar faster than any bug."""
    assert build_daily_brief("a@b.com", DailyFacts()) is None


def test_a_skipped_brief_is_a_success_not_a_failure() -> None:
    outcome, mailer, log = deliver(None)

    assert outcome.status is SendStatus.SKIPPED
    assert outcome.status.is_problem is False
    assert mailer.sent == []
    assert log.records[0]["status"] == "skipped"


def test_the_skip_is_logged_so_silence_is_visible_in_admin() -> None:
    """An email that was never sent and never recorded is indistinguishable
    from a cron that did not run."""
    _, _, log = deliver(None)
    assert log.records[0]["detail"] == "nothing to report"


def test_any_single_real_fact_is_enough_to_send() -> None:
    for facts in (
        DailyFacts(new_matches=1),
        DailyFacts(draft_ready_for="Thursday"),
        DailyFacts(score_moved="LinkedIn 58 → 62"),
        DailyFacts(interview_prep_due="Zomato"),
    ):
        assert build_daily_brief("a@b.com", facts) is not None


def test_an_unreachable_source_does_not_become_nothing_new() -> None:
    """ "We couldn't look" must never be sent as "nothing matched". With no
    real facts and a failed source we send NOTHING — silence is at least
    honest, and the failure shows up in admin."""
    brief = build_daily_brief("a@b.com", DailyFacts(sources_unavailable=("Career Scout",)))
    assert brief is None


def test_a_failed_source_is_named_in_a_brief_that_does_send() -> None:
    """Otherwise its silence reads as an all-clear."""
    brief = build_daily_brief(
        "a@b.com",
        DailyFacts(draft_ready_for="Thursday", sources_unavailable=("Career Scout",)),
    )

    assert brief is not None
    assert "couldn't reach Career Scout" in brief.body
    assert "unchecked" in brief.body


# ── Subject lines ──────────────────────────────────────────────────────────


def test_the_subject_names_the_thing_rather_than_saying_update() -> None:
    """A subject that says nothing is a subject that gets ignored."""
    brief = build_daily_brief("a@b.com", DailyFacts(new_matches=3))
    assert brief is not None
    assert brief.subject == "3 new roles matched"
    assert "update" not in brief.subject.lower()


def test_the_subject_is_singular_for_one() -> None:
    brief = build_daily_brief("a@b.com", DailyFacts(new_matches=1))
    assert brief is not None
    assert brief.subject == "1 new role matched"


# ── The tone law ───────────────────────────────────────────────────────────


def test_an_email_cannot_be_constructed_with_shaming_copy() -> None:
    """An inbox is the worst place to discover we shamed someone — they cannot
    navigate away from it."""
    with pytest.raises(GuiltyCopy):
        Email(
            to="a@b.com",
            subject="We missed you",
            body="Come back",
            email_type=EmailType.DAILY_BRIEF,
        )


def test_the_guard_covers_the_body_as_well_as_the_subject() -> None:
    with pytest.raises(GuiltyCopy):
        Email(
            to="a@b.com",
            subject="Your week",
            body="You're falling behind on posts.",
            email_type=EmailType.WEEKLY_REVIEW,
        )


def test_a_broken_streak_sentence_passes_the_guard() -> None:
    """The honest version must not be caught by the rule protecting it."""
    review = build_weekly_review(
        "a@b.com",
        WeeklyFacts(
            shipped=0,
            committed=2,
            streak_sentence="Last streak: 6 weeks. A new one starts when you ship.",
            pipeline_lines=("1 applied",),
        ),
    )
    assert review is not None
    assert "6 weeks" in review.body


# ── Preferences and unsubscribe ────────────────────────────────────────────


def test_unsubscribing_stops_the_marketing_emails() -> None:
    prefs = Preferences(unsubscribed_all=True)
    assert prefs.allows(EmailType.DAILY_BRIEF) is False
    assert prefs.allows(EmailType.WEEKLY_REVIEW) is False


def test_unsubscribing_does_not_stop_a_receipt() -> None:
    """A receipt is a record of a transaction, not marketing. Suppressing it
    would withhold proof that someone paid."""
    prefs = Preferences(unsubscribed_all=True)
    assert prefs.allows(EmailType.RECEIPT) is True


def test_a_single_toggle_silences_only_its_own_email() -> None:
    prefs = Preferences(daily_brief=False)
    assert prefs.allows(EmailType.DAILY_BRIEF) is False
    assert prefs.allows(EmailType.WEEKLY_REVIEW) is True


def test_an_unsubscribed_user_gets_nothing_sent() -> None:
    brief = build_daily_brief("a@b.com", DailyFacts(new_matches=2))
    outcome, mailer, _ = deliver(brief, prefs=Preferences(unsubscribed_all=True))

    assert outcome.status is SendStatus.UNSUBSCRIBED
    assert mailer.sent == []


# ── The duplicate guard ────────────────────────────────────────────────────


def test_the_same_brief_is_sent_once_per_period() -> None:
    brief = build_daily_brief("a@b.com", DailyFacts(new_matches=2))
    log = FakeLog()

    first, mailer, _ = deliver(brief, log=log)
    second, mailer2, _ = deliver(brief, log=log)

    assert first.status is SendStatus.SENT
    assert second.status is SendStatus.DUPLICATE
    assert len(mailer.sent) == 1
    assert mailer2.sent == []


def test_a_different_period_sends_again() -> None:
    brief = build_daily_brief("a@b.com", DailyFacts(new_matches=2))
    log = FakeLog()

    deliver(brief, log=log, period_key="2026-08-08")
    second, mailer, _ = deliver(brief, log=log, period_key="2026-08-09")

    assert second.status is SendStatus.SENT
    assert len(mailer.sent) == 1


def test_one_users_send_does_not_block_another() -> None:
    brief = build_daily_brief("a@b.com", DailyFacts(new_matches=2))
    log = FakeLog()

    deliver(brief, log=log, user_id="u1")
    second, _, _ = deliver(brief, log=log, user_id="u2")

    assert second.status is SendStatus.SENT


def test_the_period_is_claimed_before_sending() -> None:
    """A crash mid-send must not leave the period unclaimed, or the retry
    sends a second copy."""
    brief = build_daily_brief("a@b.com", DailyFacts(new_matches=2))
    log = FakeLog()

    outcome, _, _ = deliver(brief, log=log, mailer=FakeMailer(explode=True))

    assert outcome.status is SendStatus.FAILED
    # The period IS claimed, so a retry will not double-send.
    assert ("u1", "daily_brief", "2026-08-08") in log.claimed


def test_a_send_failure_is_logged_and_does_not_raise() -> None:
    """One bad address must not stop the whole cron pass."""
    brief = build_daily_brief("a@b.com", DailyFacts(new_matches=2))
    outcome, _, log = deliver(brief, mailer=FakeMailer(explode=True))

    assert outcome.status is SendStatus.FAILED
    assert log.records[-1]["status"] == "failed"


# ── Dry run ────────────────────────────────────────────────────────────────


def test_a_dry_run_sends_nothing() -> None:
    brief = build_daily_brief("a@b.com", DailyFacts(new_matches=2))
    outcome, mailer, _ = deliver(brief, dry_run=True)

    assert outcome.status is SendStatus.DRY_RUN
    assert mailer.sent == []
    assert "Would send" in outcome.detail


def test_a_dry_run_does_not_claim_the_period() -> None:
    """Otherwise a rehearsal would silently suppress the real send."""
    brief = build_daily_brief("a@b.com", DailyFacts(new_matches=2))
    log = FakeLog()

    deliver(brief, log=log, dry_run=True)
    real, mailer, _ = deliver(brief, log=log)

    assert real.status is SendStatus.SENT
    assert len(mailer.sent) == 1


def test_a_dry_run_still_skips_a_quiet_day() -> None:
    """The rehearsal must show the same skips the real run would."""
    outcome, _, _ = deliver(None, dry_run=True)
    assert outcome.status is SendStatus.SKIPPED


# ── The weekly review ──────────────────────────────────────────────────────


def test_the_weekly_review_carries_the_approve_prompt() -> None:
    review = build_weekly_review(
        "a@b.com",
        WeeklyFacts(week_label="03-09 Aug", shipped=2, committed=2, contract_pending=True),
    )
    assert review is not None
    assert "/progress" in review.body
    assert "approve" in review.body.lower()


def test_a_pending_contract_alone_is_worth_an_email() -> None:
    """Unlike the daily brief: an unsigned contract IS something that needs
    the user, even in a week where nothing else happened."""
    review = build_weekly_review("a@b.com", WeeklyFacts(contract_pending=True))
    assert review is not None


def test_a_completely_empty_week_still_sends_nothing() -> None:
    assert build_weekly_review("a@b.com", WeeklyFacts()) is None


def test_the_review_subject_carries_the_number() -> None:
    review = build_weekly_review("a@b.com", WeeklyFacts(shipped=2, committed=2))
    assert review is not None
    assert review.subject == "Your week — 2/2 shipped"


# ── Period keys ────────────────────────────────────────────────────────────


def test_the_daily_key_is_the_date() -> None:
    assert daily_key(date(2026, 8, 8)) == "2026-08-08"


def test_a_sunday_job_and_a_monday_retry_share_a_weekly_key() -> None:
    """The whole point of a weekly key, and it took three goes to get here.

    `weekly_key` used to return an ISO week number, and its docstring said this
    property held. It did not: ISO weeks END on Sunday, so the Sunday send was
    W34 and the Monday retry was W35, and the retry sent a second review.

    **This test used to assert the opposite** — "must NOT collapse — they are
    different ISO weeks. This pins the actual behaviour so nobody assumes
    otherwise." So the function claimed one thing, the test pinned the other,
    and both were green. A test written to describe behaviour it has not
    questioned records the bug as the specification.

    (The value was also unstorable — see `tests/test_period_keys.py`.)
    """
    sunday = date(2026, 8, 9)
    assert sunday.strftime("%A") == "Sunday"

    assert weekly_key(sunday) == weekly_key(date(2026, 8, 10)), "Monday retry, same claim"
    assert weekly_key(date(2026, 8, 8)) != weekly_key(sunday), "the Saturday before is prior week"
    assert weekly_key(sunday) == "2026-08-09"


def test_the_weekly_key_survives_a_year_boundary() -> None:
    """31 Dec 2026 and 1 Jan 2027 fall in one week and must share one key.

    Anchoring to the week's Sunday gets this for free, and without the ISO
    year/week mismatch that made the old key read `2026-W53` in January.
    """
    assert weekly_key(date(2026, 12, 31)) == weekly_key(date(2027, 1, 1))
    assert weekly_key(date(2027, 1, 1)) == "2026-12-27"


# ── The run summary ────────────────────────────────────────────────────────


def test_skips_are_reported_as_a_number_not_omitted() -> None:
    """ "12 sent, 40 skipped" is the system working as designed. Hiding the
    skips would make a healthy quiet day look like a broken cron."""
    summary = RunSummary()
    for _ in range(12):
        summary.record(SendOutcome(SendStatus.SENT), "a@b.com")
    for _ in range(40):
        summary.record(SendOutcome(SendStatus.SKIPPED), "b@b.com")

    assert "12 sent" in summary.line()
    assert "40 skipped (nothing to say)" in summary.line()


def test_failures_are_shouted_not_buried() -> None:
    summary = RunSummary()
    summary.record(SendOutcome(SendStatus.SENT), "a@b.com")
    summary.record(SendOutcome(SendStatus.FAILED, "smtp down"), "bad@b.com")

    assert "1 FAILED" in summary.line()
    assert summary.failures == ["bad@b.com: smtp down"]


def test_a_clean_run_reports_no_failure_section() -> None:
    summary = RunSummary()
    summary.record(SendOutcome(SendStatus.SENT), "a@b.com")

    assert "FAILED" not in summary.line()
    assert summary.failures == []


# ── SMTP transport: the port decides the handshake ─────────────────────────
# Hostinger's credentials are port 465, which is implicit TLS. The mailer
# originally only did the 587 STARTTLS flow and would have failed on the first
# real send — as a TIMEOUT, not a clear error, which is the worst kind.


def _mailer(port: int) -> Any:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from send_emails import SmtpMailer  # type: ignore[import-not-found]

    from carigma_api.config import Settings

    return SmtpMailer(Settings(smtp_host="smtp.hostinger.com", smtp_port=port))


def test_port_465_uses_implicit_tls() -> None:
    assert _mailer(465).uses_implicit_tls is True


def test_port_587_uses_starttls() -> None:
    assert _mailer(587).uses_implicit_tls is False


def test_465_opens_an_ssl_socket_and_never_calls_starttls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calling starttls() on an already-encrypted 465 connection is the exact
    mistake this guards. Verified by asserting it is NOT called."""
    import smtplib

    calls: list[str] = []

    class FakeSSL:
        def __init__(self, host: str, port: int, timeout: int = 0) -> None:
            calls.append(f"SMTP_SSL:{port}")

        def starttls(self) -> None:
            calls.append("starttls")

        def login(self, u: str, p: str) -> None:
            calls.append("login")

    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSSL)
    monkeypatch.setattr(smtplib, "SMTP", lambda *a, **k: pytest.fail("465 must not use plain SMTP"))

    _mailer(465)._connect()

    assert calls == ["SMTP_SSL:465", "login"]
    assert "starttls" not in calls


def test_587_connects_plaintext_then_upgrades(monkeypatch: pytest.MonkeyPatch) -> None:
    import smtplib

    calls: list[str] = []

    class FakePlain:
        def __init__(self, host: str, port: int, timeout: int = 0) -> None:
            calls.append(f"SMTP:{port}")

        def starttls(self) -> None:
            calls.append("starttls")

        def login(self, u: str, p: str) -> None:
            calls.append("login")

    monkeypatch.setattr(smtplib, "SMTP", FakePlain)
    monkeypatch.setattr(
        smtplib, "SMTP_SSL", lambda *a, **k: pytest.fail("587 must not use SMTP_SSL")
    )

    _mailer(587)._connect()

    # STARTTLS must happen BEFORE login, or credentials cross in plaintext.
    assert calls == ["SMTP:587", "starttls", "login"]
