"""Emails — the retention loop.

Streamlit could not schedule anything, so the daily brief and weekly review are
genuinely new capability rather than a port. They are also the loop that brings
people back, which makes them the easiest place in the product to do real
damage.

## The rule that matters most: skip when nothing happened

**A daily brief with nothing in it is not a small annoyance — it is a lie by
implication.** It says "something needs you" when nothing does. Wednesday is
quiet by design; the product's whole claim is that it only speaks when it has
something. An email that manufactures urgency breaks that faster than any bug,
and it trains people to ignore the ones that matter.

So `build_daily_brief` returns `None` when there is nothing real, and the
sender treats `None` as a **successful skip** rather than a failure.

## The lapsed grammar applies here too

Facts only, banned strings, decay reported as weather. An inbox is the easiest
place to accidentally shame someone into leaving, and unlike an in-app surface
they cannot navigate away from it — it sits in their morning.

Every rendered email passes through `assert_no_guilt` before it can be sent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Protocol

from carigma_api.services.weekly import assert_no_guilt

logger = logging.getLogger(__name__)

FROM_ADDRESS = "Carigma <support@carigma.in>"


class EmailType(StrEnum):
    WELCOME = "welcome"
    DAILY_BRIEF = "daily_brief"
    WEEKLY_REVIEW = "weekly_review"
    RECEIPT = "receipt"

    @property
    def is_marketing(self) -> bool:
        """Which emails an unsubscribe silences.

        A receipt is a record of a transaction, not marketing — unsubscribing
        from "emails" must not stop someone receiving proof they paid.
        """
        return self in (EmailType.DAILY_BRIEF, EmailType.WEEKLY_REVIEW)


class SendStatus(StrEnum):
    SENT = "sent"
    FAILED = "failed"
    #: Nothing real to say. A SUCCESS, not a failure — see the module note.
    SKIPPED = "skipped"
    #: Already sent this period. The duplicate guard held.
    DUPLICATE = "duplicate"
    #: The user turned this off, or unsubscribed.
    UNSUBSCRIBED = "unsubscribed"
    DRY_RUN = "dry_run"

    @property
    def is_problem(self) -> bool:
        """Only a genuine failure is a problem. Skips and duplicates are the
        system working."""
        return self is SendStatus.FAILED


@dataclass(frozen=True)
class Email:
    to: str
    subject: str
    body: str
    email_type: EmailType

    def __post_init__(self) -> None:
        # The tone law, enforced before anything can leave. An inbox is the
        # worst place to discover we shamed someone.
        assert_no_guilt(self.subject, self.body)


# ── The daily brief ────────────────────────────────────────────────────────


@dataclass
class DailyFacts:
    """What actually happened since the last brief.

    Deliberately all-optional and all-falsy-by-default, so `has_anything` is
    the honest question rather than something a caller has to remember to ask.
    """

    new_matches: int = 0
    draft_ready_for: str | None = None
    score_moved: str | None = None
    interview_prep_due: str | None = None
    #: Set when a data source could not be reached. NOT the same as "nothing
    #: happened" — see below.
    sources_unavailable: tuple[str, ...] = ()

    @property
    def has_anything(self) -> bool:
        return bool(
            self.new_matches or self.draft_ready_for or self.score_moved or self.interview_prep_due
        )


def build_daily_brief(email: str, facts: DailyFacts, *, greeting_name: str = "") -> Email | None:
    """Return the brief, or **None when there is nothing real to say**.

    The `sources_unavailable` case is the subtle one. If Career Scout could not
    reach a provider, we do NOT know whether there were new matches — and
    "we couldn't look" must never be sent as "nothing new". Rather than send a
    misleading brief, we send nothing: silence is at least honest, and the
    failure is visible in admin where it can be acted on.
    """
    if not facts.has_anything:
        if facts.sources_unavailable:
            logger.warning(
                "daily brief skipped for %s with unreachable sources: %s",
                email,
                facts.sources_unavailable,
            )
        return None

    lines: list[str] = []
    if facts.new_matches:
        s = "role" if facts.new_matches == 1 else "roles"
        lines.append(f"· {facts.new_matches} new {s} matched your bar.")
    if facts.draft_ready_for:
        lines.append(f"· Your {facts.draft_ready_for} post is drafted and waiting.")
    if facts.score_moved:
        lines.append(f"· {facts.score_moved}")
    if facts.interview_prep_due:
        lines.append(f"· Interview prep for {facts.interview_prep_due}.")

    # If a source failed, say so IN the brief rather than letting its silence
    # read as an all-clear.
    if facts.sources_unavailable:
        which = " and ".join(facts.sources_unavailable)
        lines.append(f"· We couldn't reach {which} this morning, so that part is unchecked.")

    hello = f"Morning, {greeting_name}." if greeting_name else "Morning."
    return Email(
        to=email,
        subject=_daily_subject(facts),
        body=f"{hello}\n\n" + "\n".join(lines) + "\n\nOpen Carigma: https://carigma.in/today\n",
        email_type=EmailType.DAILY_BRIEF,
    )


def _daily_subject(facts: DailyFacts) -> str:
    """The single most important thing, named. Never "Your daily update" —
    a subject that says nothing is a subject that gets ignored."""
    if facts.new_matches:
        s = "role" if facts.new_matches == 1 else "roles"
        return f"{facts.new_matches} new {s} matched"
    if facts.draft_ready_for:
        return f"Your {facts.draft_ready_for} post is ready"
    if facts.score_moved:
        return facts.score_moved
    if facts.interview_prep_due:
        return f"Prep for {facts.interview_prep_due}"
    return "Carigma"


# ── The weekly review ──────────────────────────────────────────────────────


@dataclass
class WeeklyFacts:
    week_label: str = ""
    shipped: int = 0
    committed: int = 0
    score_lines: tuple[str, ...] = ()
    pipeline_lines: tuple[str, ...] = ()
    recommendation: str = ""
    streak_sentence: str = ""
    contract_pending: bool = False

    @property
    def has_anything(self) -> bool:
        return bool(
            self.shipped or self.score_lines or self.pipeline_lines or self.contract_pending
        )


def build_weekly_review(email: str, facts: WeeklyFacts) -> Email | None:
    """Sunday's review. Screenshot-bait by design, and it carries the
    approve-next-week prompt.

    Unlike the daily brief this survives a quiet week — an unsigned contract is
    itself something that needs the user. But a week where literally nothing
    happened and no contract is waiting still sends nothing.
    """
    if not facts.has_anything:
        return None

    parts: list[str] = [f"Your week — {facts.week_label}".rstrip(" —")]
    if facts.committed:
        parts.append(f"\n{facts.shipped}/{facts.committed} posts shipped.")
    for line in facts.score_lines:
        parts.append(f"· {line}")
    for line in facts.pipeline_lines:
        parts.append(f"· {line}")
    if facts.streak_sentence:
        parts.append(f"\n{facts.streak_sentence}")
    if facts.recommendation:
        parts.append(f"\nOne thing for next week: {facts.recommendation}")
    if facts.contract_pending:
        parts.append("\nNext week's plan is ready to approve:\nhttps://carigma.in/progress")

    return Email(
        to=email,
        subject=f"Your week — {facts.shipped}/{facts.committed} shipped"
        if facts.committed
        else "Your week",
        body="\n".join(parts) + "\n",
        email_type=EmailType.WEEKLY_REVIEW,
    )


# ── Sending ────────────────────────────────────────────────────────────────


class Mailer(Protocol):
    def send(self, email: Email) -> None: ...


class SendLog(Protocol):
    def record(
        self,
        email: Email | None,
        *,
        recipient: str,
        email_type: str,
        status: str,
        detail: str | None,
    ) -> None: ...

    def claim_period(self, user_id: str, email_type: str, period_key: str) -> bool:
        """Reserve this (user, type, period). False means already sent.

        Backed by a UNIQUE INDEX, not an application check — two cron
        processes racing must not both send.
        """
        ...


@dataclass
class SendOutcome:
    status: SendStatus
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"status": str(self.status), "detail": self.detail}


@dataclass
class Preferences:
    """Settings toggles, plus the global unsubscribe."""

    daily_brief: bool = True
    weekly_review: bool = True
    unsubscribed_all: bool = False

    def allows(self, email_type: EmailType) -> bool:
        # A receipt is a transaction record, not marketing. Unsubscribing must
        # not stop someone receiving proof they paid.
        if not email_type.is_marketing:
            return True
        if self.unsubscribed_all:
            return False
        return self.daily_brief if email_type is EmailType.DAILY_BRIEF else self.weekly_review


def send(
    email: Email | None,
    *,
    mailer: Mailer,
    log: SendLog,
    user_id: str,
    recipient: str,
    email_type: EmailType,
    period_key: str,
    prefs: Preferences,
    dry_run: bool = False,
) -> SendOutcome:
    """The one path from a built email to an actually-sent one.

    Order matters and is deliberate:
    1. **Nothing to say** → skip. Checked first, so a quiet day never even
       consults preferences or burns a period claim.
    2. **Preferences** → unsubscribed.
    3. **Duplicate guard** → claim the period BEFORE sending, so a crash
       mid-send cannot produce a second attempt that succeeds.
    4. **Dry run** → print, claim nothing, send nothing.
    """
    if email is None:
        log.record(
            None,
            recipient=recipient,
            email_type=str(email_type),
            status=str(SendStatus.SKIPPED),
            detail="nothing to report",
        )
        return SendOutcome(SendStatus.SKIPPED, "Nothing real happened — no email sent.")

    if not prefs.allows(email_type):
        log.record(
            email,
            recipient=recipient,
            email_type=str(email_type),
            status=str(SendStatus.UNSUBSCRIBED),
            detail=None,
        )
        return SendOutcome(SendStatus.UNSUBSCRIBED)

    if dry_run:
        # Deliberately does NOT claim the period: a dry run must be repeatable,
        # and claiming would silently suppress the real send afterwards.
        logger.info("[dry-run] to=%s subject=%s", email.to, email.subject)
        log.record(
            email,
            recipient=recipient,
            email_type=str(email_type),
            status=str(SendStatus.DRY_RUN),
            detail=None,
        )
        return SendOutcome(SendStatus.DRY_RUN, f"Would send: {email.subject}")

    if not log.claim_period(user_id, str(email_type), period_key):
        return SendOutcome(SendStatus.DUPLICATE, "Already sent this period.")

    try:
        mailer.send(email)
    except Exception as exc:  # noqa: BLE001 — one bad address must not stop the run
        logger.exception("email send failed for %s", recipient)
        log.record(
            email,
            recipient=recipient,
            email_type=str(email_type),
            status=str(SendStatus.FAILED),
            detail=str(exc)[:200],
        )
        return SendOutcome(SendStatus.FAILED, "Send failed.")

    log.record(
        email,
        recipient=recipient,
        email_type=str(email_type),
        status=str(SendStatus.SENT),
        detail=None,
    )
    return SendOutcome(SendStatus.SENT)


# ── Period keys ────────────────────────────────────────────────────────────


def daily_key(day: date) -> str:
    return day.isoformat()


def weekly_key(day: date) -> str:
    """ISO week, so a Sunday-evening job and a Monday-morning retry of the same
    week collapse to one send rather than two."""
    iso = day.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


# ── Run summary ────────────────────────────────────────────────────────────


@dataclass
class RunSummary:
    """What a cron pass actually did. Reported honestly: skips are a category,
    not a silence."""

    counts: dict[str, int] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)

    def record(self, outcome: SendOutcome, recipient: str) -> None:
        key = str(outcome.status)
        self.counts[key] = self.counts.get(key, 0) + 1
        if outcome.status.is_problem:
            self.failures.append(f"{recipient}: {outcome.detail}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "counts": self.counts,
            "failures": self.failures,
            "line": self.line(),
            "ran_at": datetime.now().isoformat(timespec="seconds"),
        }

    def line(self) -> str:
        sent = self.counts.get("sent", 0)
        skipped = self.counts.get("skipped", 0)
        failed = self.counts.get("failed", 0)
        # Skipped is reported as a first-class number, not omitted. "12 sent,
        # 40 skipped" is the system working as designed.
        parts = [f"{sent} sent", f"{skipped} skipped (nothing to say)"]
        # Every other outcome gets named too. A dry run that reported
        # "0 sent · 0 skipped" was hiding the one thing it actually did.
        for key, label in (
            ("dry_run", "would send"),
            ("duplicate", "already sent this period"),
            ("unsubscribed", "unsubscribed"),
        ):
            if n := self.counts.get(key, 0):
                parts.append(f"{n} {label}")
        if failed:
            parts.append(f"{failed} FAILED")
        return " · ".join(parts)
