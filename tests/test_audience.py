"""This deploy may not email whoever happens to be in the database.

## Why the gate exists

`_recipients` reads every row in `profiles` joined to its auth email. V1 and V2
share one Supabase, so that set is **every live V1 user** — twelve people who
never signed up for V2 mail.

Nothing has reached them, and the reason is an accident: `weekly_key` produced
`"2026-W34"` for a `date` column, `claim_period` read Postgres's refusal as
"already claimed", and the weekly path was inert whatever the cron flag said.
Fixing that removed the only thing standing between the cron and their inboxes.

**An unintended safety is one that leaves without notice.** This is the
deliberate replacement.

## The shape, and why it is this shape

A V2-only FILTER was the other candidate and cannot be built honestly: nothing
in `profiles` separates a V1 user from a V2 one — `onboarded` is written by
both — so any filter would be a guess wearing the clothes of a rule.

An allow-list is a fact. And it is defaulted CLOSED: `send()` builds an empty
`Audience` when none is passed, so a caller who forgets emails nobody. The
previous accident failed toward silence too; the difference is that this one
does it on purpose, says so in the run summary, and cannot be removed without
someone typing `EMAIL_AUDIENCE=everyone`.
"""

from __future__ import annotations

from typing import Any

import pytest

from carigma_api.services.emails import (
    Audience,
    Email,
    EmailType,
    Preferences,
    RunSummary,
    SendOutcome,
    SendStatus,
    send,
)


class Mailer:
    def __init__(self) -> None:
        self.sent: list[Email] = []

    def send(self, email: Email) -> None:
        self.sent.append(email)


class Log:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []
        self.claims: list[tuple[str, str, str]] = []

    def record(self, email: Email | None, **kw: Any) -> None:
        self.records.append(kw)

    def claim_period(self, user_id: str, email_type: str, period_key: str) -> bool:
        self.claims.append((user_id, email_type, period_key))
        return True


def deliver(**over: Any) -> tuple[SendOutcome, Mailer, Log]:
    mailer, log = Mailer(), Log()
    outcome = send(
        Email(to="someone@example.com", subject="s", body="b", email_type=EmailType.DAILY_BRIEF),
        mailer=mailer,
        log=log,
        user_id="11111111-1111-1111-1111-111111111111",
        recipient=over.pop("recipient", "someone@example.com"),
        email_type=EmailType.DAILY_BRIEF,
        period_key="2026-08-23",
        prefs=Preferences(),
        service_key="test-signing-key",
        app_url="https://app.example.com",
        **over,
    )
    return outcome, mailer, log


# ── The default ────────────────────────────────────────────────────────────


def test_no_audience_at_all_emails_nobody() -> None:
    """The property the whole design rests on.

    A caller who forgets must send NOTHING. The alternative default sends to
    everyone, which is exactly the failure this replaced.
    """
    outcome, mailer, _log = deliver()

    assert outcome.status is SendStatus.NOT_IN_AUDIENCE
    assert mailer.sent == []


def test_an_empty_allowlist_emails_nobody() -> None:
    outcome, mailer, _log = deliver(audience=Audience.from_settings("allowlist", ""))

    assert outcome.status is SendStatus.NOT_IN_AUDIENCE
    assert mailer.sent == []


def test_the_settings_defaults_admit_nobody() -> None:
    """Read from the shipped defaults, not from a literal here.

    A test that retypes the default is a second copy of it, and would keep
    passing after someone changed the real one.
    """
    from carigma_api.config import Settings

    settings = Settings()
    audience = Audience.from_settings(settings.email_audience, settings.email_allowlist)

    assert not audience.admits("anyone@example.com")


# ── Letting people through ─────────────────────────────────────────────────


def test_an_allow_listed_address_goes_through() -> None:
    audience = Audience.from_settings("allowlist", "someone@example.com")
    outcome, mailer, _log = deliver(audience=audience)

    assert outcome.status is SendStatus.SENT
    assert len(mailer.sent) == 1


def test_everyone_means_everyone() -> None:
    outcome, mailer, _log = deliver(audience=Audience.from_settings("everyone", ""))

    assert outcome.status is SendStatus.SENT
    assert len(mailer.sent) == 1


@pytest.mark.parametrize(
    "configured",
    ["Someone@Example.com", " someone@example.com ", "other@x.com, someone@example.com"],
)
def test_the_allowlist_is_forgiving_about_case_and_spacing(configured: str) -> None:
    """An address that fails to match because of a capital letter is a silent
    exclusion, and this gate's failure mode is silence."""
    assert Audience.from_settings("allowlist", configured).admits("someone@example.com")


def test_an_unknown_audience_value_is_treated_as_an_allowlist() -> None:
    """A typo in the env var must not open the gate.

    `EMAIL_AUDIENCE=everyone ` with a stray character, or `all`, or `true` —
    none of those are "everyone". Only the exact word is.
    """
    for value in ("all", "true", "EVERYBODY", "", "everyone!"):
        assert not Audience.from_settings(value, "").admits("someone@example.com")

    # ...and the exact word still works, including odd casing and spacing,
    # because that is a configuration people type by hand.
    assert Audience.from_settings("  Everyone  ", "").admits("someone@example.com")


# ── How a refusal is reported ──────────────────────────────────────────────


def test_a_refusal_is_recorded_and_is_not_a_skip() -> None:
    """A skip means there was nothing to say. Here there was.

    Both bugs behind this work shared one shape: a failure that read as an
    ordinary quiet outcome. A gate whose refusal logged as `skipped` would be
    the third.
    """
    _outcome, _mailer, log = deliver()

    assert len(log.records) == 1
    assert log.records[0]["status"] == "not_in_audience"
    assert log.records[0]["status"] != str(SendStatus.SKIPPED)


def test_a_refused_recipient_does_not_burn_the_period_claim() -> None:
    """Ordering, and it matters.

    `digest_log` allows one claim per user per period. If the gate ran AFTER
    the claim, adding someone to the allow-list would find their slot already
    taken by a send that never happened — and the second attempt would report
    DUPLICATE, which is the exact confusion this whole thread is about.
    """
    _outcome, _mailer, log = deliver()

    assert log.claims == [], "a refusal consumed the day's claim"


def test_the_run_summary_names_it_loudly() -> None:
    """A run that emailed nobody must not read like a quiet day."""
    summary = RunSummary()
    for _ in range(12):
        summary.record(SendOutcome(SendStatus.NOT_IN_AUDIENCE, "x"), "a@b.com")

    line = summary.line()
    assert "12" in line
    assert "NOT IN AUDIENCE" in line


# ── Structural ─────────────────────────────────────────────────────────────


def test_both_cron_senders_build_an_audience() -> None:
    """The default is fail-closed, so forgetting is safe rather than dangerous.

    But a sender that forgets sends nothing at all, which would be a real
    outage discovered by someone not receiving mail. Checking the call sites
    turns that into a test failure instead.
    """
    import ast
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "scripts" / "send_emails.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)

    senders = [
        fn
        for fn in ast.walk(tree)
        if isinstance(fn, ast.FunctionDef)
        and any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "send"
            for n in ast.walk(fn)
        )
    ]
    assert senders, "no sender found in send_emails.py — this guard would prove nothing"

    for fn in senders:
        binds = any(
            isinstance(n, ast.Name) and n.id == "audience" and isinstance(n.ctx, ast.Store)
            for n in ast.walk(fn)
        )
        assert binds, (
            f"{fn.name} calls send() but never builds an audience. It would email nobody, "
            "and the first sign would be someone not receiving mail."
        )
