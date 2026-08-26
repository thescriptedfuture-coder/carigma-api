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
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol

from carigma_api.services.unsubscribe import link as unsubscribe_link
from carigma_api.services.weekly import assert_no_guilt

logger = logging.getLogger(__name__)

FROM_ADDRESS = "Carigma <support@carigma.in>"


class EmailType(StrEnum):
    WELCOME = "welcome"
    DAILY_BRIEF = "daily_brief"
    WEEKLY_REVIEW = "weekly_review"
    #: The lapsed-user market digest (P6-3). Marketing, and governed by the
    #: SAME toggle as the weekly review: someone who turned that off while
    #: active has already said no to a weekly email, and lapsing is not consent.
    MARKET_DIGEST = "market_digest"
    #: Something specific happened (P6-5). ONE type for all five triggers, not
    #: five types: five toggles would be five things to turn off, and five
    #: claim namespaces would be five emails a day. See `services/triggers`.
    EVENT = "event"
    RECEIPT = "receipt"

    @property
    def is_marketing(self) -> bool:
        """Which emails an unsubscribe silences.

        A receipt is a record of a transaction, not marketing — unsubscribing
        from "emails" must not stop someone receiving proof they paid.
        """
        return self in (
            EmailType.DAILY_BRIEF,
            EmailType.WEEKLY_REVIEW,
            EmailType.MARKET_DIGEST,
            EmailType.EVENT,
        )


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
    #: A marketing email we could not put an unsubscribe link in, so we did not
    #: send it. See `send()` — refusing is the safe direction.
    NO_UNSUBSCRIBE_LINK = "no_unsubscribe_link"
    #: This deploy is not allowed to email this person. NOT a skip: a skip
    #: means there was nothing to say, and this means there was.
    NOT_IN_AUDIENCE = "not_in_audience"

    @property
    def is_problem(self) -> bool:
        """Only a genuine failure is a problem. Skips and duplicates are the
        system working."""
        return self in (SendStatus.FAILED, SendStatus.NO_UNSUBSCRIBE_LINK)


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
        # An event email is a daily-brief-shaped thing: "here is what happened".
        # Someone who turned that off has already answered the question, and a
        # separate toggle would be a second chance to reach them after a no.
        if email_type in (EmailType.DAILY_BRIEF, EmailType.EVENT):
            return self.daily_brief
        return self.weekly_review


@dataclass(frozen=True)
class Audience:
    """Who this deploy is permitted to email.

    `_recipients` reads every profile in a Supabase that V1 and V2 share, so
    "who is in the database" is not the same question as "who agreed to hear
    from V2". Nothing in `profiles` distinguishes them — `onboarded` is written
    by both — so a V2-only FILTER cannot be built honestly. An explicit
    allow-list can.

    **The default admits nobody.** `send()` builds this itself when no audience
    is passed, so a caller that forgets sends nothing rather than everything —
    the opposite of what the last accident did.
    """

    everyone: bool = False
    allowed: frozenset[str] = frozenset()

    def admits(self, recipient: str) -> bool:
        return self.everyone or recipient.strip().lower() in self.allowed

    @property
    def describe(self) -> str:
        if self.everyone:
            return "everyone"
        return f"{len(self.allowed)} allow-listed address(es)"

    @classmethod
    def unrestricted(cls) -> Audience:
        """Every recipient. A deliberate act, named so it reads as one."""
        return cls(everyone=True)

    @classmethod
    def from_settings(cls, audience: str, allowlist: str) -> Audience:
        if audience.strip().lower() == "everyone":
            return cls.unrestricted()
        return cls(allowed=frozenset(a.strip().lower() for a in allowlist.split(",") if a.strip()))


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
    claim_as: str | None = None,
    service_key: str = "",
    app_url: str = "",
    audience: Audience | None = None,
) -> SendOutcome:
    """The one path from a built email to an actually-sent one.

    Order matters and is deliberate:
    1. **Nothing to say** → skip. Checked first, so a quiet day never even
       consults preferences or burns a period claim.
    2. **Preferences** → unsubscribed.
    3. **Duplicate guard** → claim the period BEFORE sending, so a crash
       mid-send cannot produce a second attempt that succeeds. `claim_as`
       shares one namespace across types where the ceiling is shared.
    4. **Audience** → this deploy may not write to this person at all.
    5. **Dry run** → print, claim nothing, send nothing.

    The audience check sits BEFORE the dry run on purpose. A dry run should
    show exactly who would be excluded; the duplicate claim cannot be
    rehearsed, but this can, and the last failure of this kind hid precisely
    because four clean dry runs exercised none of it.
    """
    if email is None:
        # A DRY RUN WRITES NOTHING, including this.
        #
        # The skip record came before the dry-run check, so `--dry-run` on a
        # quiet day wrote a skip row per user — twelve of them, describing a
        # run that never happened. Repeated dry runs accumulate them, and
        # anybody later counting skips to reason about behaviour would be
        # counting rehearsals as events.
        #
        # "What would we send?" must be free to ask. Same rule as the referral
        # producer being read-only.
        if not dry_run:
            log.record(
                None,
                recipient=recipient,
                email_type=str(email_type),
                status=str(SendStatus.SKIPPED),
                detail="nothing to report",
            )
        return SendOutcome(SendStatus.SKIPPED, "Nothing real happened — no email sent.")

    # Marketing mail carries an unsubscribe link, or it does not go.
    #
    # Until now NOTHING did — every proactive email V2 sent was unsubscribe-less
    # marketing mail. That is the compliance half of the public unsubscribe
    # route, and the route existing does not help anyone who never receives a
    # link to it.
    #
    # Built HERE rather than in each builder because this is the one path from
    # a built email to a sent one: a sixth email type cannot forget, and there
    # is no "I'll add the footer later" state that still sends.
    #
    # Refusing rather than sending without it is the safe direction. A
    # misconfigured deploy stops mailing instead of mailing unlawfully — the
    # same shape as JSEARCH_DAILY_CAP producing a question rather than a bill.
    if email is not None and email_type.is_marketing:
        try:
            footer_link = unsubscribe_link(user_id, service_key=service_key, app_url=app_url)
        except Exception:
            logger.error(
                "REFUSING to send %s to %s: no unsubscribe link could be built. "
                "Marketing mail without one is not something we send.",
                email_type,
                recipient,
            )
            log.record(
                email,
                recipient=recipient,
                email_type=str(email_type),
                status=str(SendStatus.NO_UNSUBSCRIBE_LINK),
                detail="no signing key or app url",
            )
            return SendOutcome(SendStatus.NO_UNSUBSCRIBE_LINK, "No unsubscribe link — not sent.")
        email = replace(email, body=email.body.rstrip() + "\n\n" + footer(footer_link))

    if not prefs.allows(email_type):
        log.record(
            email,
            recipient=recipient,
            email_type=str(email_type),
            status=str(SendStatus.UNSUBSCRIBED),
            detail=None,
        )
        return SendOutcome(SendStatus.UNSUBSCRIBED)

    # The audience gate. Built here rather than taken on trust, so omitting it
    # admits NOBODY — see `Audience`.
    if not (audience or Audience()).admits(recipient):
        log.record(
            email,
            recipient=recipient,
            email_type=str(email_type),
            status=str(SendStatus.NOT_IN_AUDIENCE),
            detail=(audience or Audience()).describe,
        )
        return SendOutcome(
            SendStatus.NOT_IN_AUDIENCE,
            "This deploy is not permitted to email that address.",
        )

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

    # `claim_as` lets several email TYPES share one claim namespace, which is
    # how "more reasons, never more frequency" becomes a database constraint
    # rather than a rule someone remembers. The daily brief and all five event
    # triggers pass `triggers.DAILY_SLOT`, so whichever runs first wins the
    # day and the rest are DUPLICATE. A trigger added next year cannot raise
    # anyone's volume, because the index does not know how many triggers exist.
    if not log.claim_period(user_id, claim_as or str(email_type), period_key):
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


# ── Which email wins the day ───────────────────────────────────────────────

#: The order proactive emails are ATTEMPTED in, most important first.
#:
#: The daily slot guarantees one email per user per day. It does not decide
#: WHICH — and "whichever cron ran first" is not a decision, it is an accident
#: of how two schedule entries happened to be written. On a Sunday where a
#: review, a brief and an event all qualify, the user's inbox should not depend
#: on that.
#:
#: The ranking:
#:
#: 1. **The weekly review.** Once a week, it is the promised artefact, and a
#:    missed one means that week has no record at all. Everything below it
#:    recurs.
#: 2. **An event.** Something specific happened to THEM — a closing follow-up
#:    window, an approval waiting, a band they moved.
#: 3. **The daily brief.** The general roundup. Real, and the most repeatable
#:    thing here, so it yields to anything specific.
#: 4. **The market digest.** Lapsed users only, and by construction the least
#:    time-sensitive thing we send: someone who has not been back in six weeks
#:    is not waiting on today's edition.
#:
#: Reviewed as a list, and pinned by a test. A reordering should require
#: someone to disagree with the reasoning above, not to notice a subtle change
#: in a scheduler.
PROACTIVE_ORDER: tuple[EmailType, ...] = (
    EmailType.WEEKLY_REVIEW,
    EmailType.EVENT,
    EmailType.DAILY_BRIEF,
    EmailType.MARKET_DIGEST,
)


def outranks(a: EmailType, b: EmailType) -> bool:
    """True when `a` should win the day against `b`.

    Raises on a type that is not proactive rather than answering False: a
    receipt is not competing for the slot at all, and silently ranking it
    would be a wrong answer to a question nobody should have asked.
    """
    if a not in PROACTIVE_ORDER or b not in PROACTIVE_ORDER:
        raise ValueError(f"not a proactive email type: {a if a not in PROACTIVE_ORDER else b}")
    return PROACTIVE_ORDER.index(a) < PROACTIVE_ORDER.index(b)


def footer(link: str) -> str:
    """One line, plain, and the link is the whole affordance.

    No "we're sad to see you go" and no confirmation step in the copy — the
    route is one click by design, and asking someone to reconsider on the way
    out is the dark pattern this product does not do.
    """
    return (
        "—\n"
        f"Stop these emails: {link}\n"
        "Receipts and anything you ask us for directly are unaffected."
    )


# ── Period keys ────────────────────────────────────────────────────────────
#
# Both of these end up in `digest_log.sent_on`, which is `date not null`. That
# is the whole constraint on their shape, and it was violated for months:
# `weekly_key` returned `"2026-W34"`, Postgres answers that with
#
#     ERROR: 22007: invalid input syntax for type date: "2026-W34"
#
# and `claim_period` catches every exception and returns False — which means
# "already sent". **So every weekly review and every re-engagement digest was
# claimed, refused, and reported as a benign duplicate.** Nothing errored,
# nothing sent, and a dry run could not show it because `send` returns before
# it claims.
#
# `tests/test_period_keys.py` now checks the output of every function here
# against the column it lands in.


def daily_key(day: date) -> str:
    return day.isoformat()


def weekly_key(day: date) -> str:
    """The most recent Sunday on or before `day`, as an ISO date.

    Two jobs, and the old implementation did neither.

    **Storable.** `date.fromisoformat` accepts this and so does Postgres. An
    ISO week string is not a date in any format the column understands.

    **Idempotent across the Sunday/Monday boundary.** The point of a weekly key
    is that a Sunday-evening send and a Monday-morning retry of the same run
    collapse to one email. An ISO WEEK number cannot do that — ISO weeks END on
    Sunday, so Sunday is `W34` and the Monday after it is `W35`, and the retry
    would have sent a second review. The old docstring claimed this property;
    it never had it.

    Anchoring to the Sunday gives it: Sunday returns itself, and the following
    Monday returns the same Sunday. `weekday()` is Mon=0 … Sun=6, so
    `(weekday + 1) % 7` is the number of days back to that Sunday.
    """
    return (day - timedelta(days=(day.weekday() + 1) % 7)).isoformat()


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
            # Named loudly. A run that emailed nobody because of the audience
            # gate must not read like a quiet day.
            ("not_in_audience", "NOT IN AUDIENCE"),
        ):
            if n := self.counts.get(key, 0):
                parts.append(f"{n} {label}")
        if failed:
            parts.append(f"{failed} FAILED")
        return " · ".join(parts)
