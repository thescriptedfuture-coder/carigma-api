"""Render Cron entrypoint for the daily brief and weekly review.

    python scripts/send_emails.py daily   --dry-run
    python scripts/send_emails.py weekly

**`--dry-run` prints and sends nothing, and claims no period**, so a rehearsal
never suppresses the real send that follows. It is the default in any
environment where SMTP is not configured, because a cron that silently does
nothing is worse than one that says it would have.

Scheduling (Render Cron, both IST):
    daily   — 08:30 IST  =>  `0 3 * * *`   UTC
    weekly  — 18:00 IST  =>  `30 12 * * 0` UTC
"""

from __future__ import annotations

import argparse
import json
import logging
import smtplib
import sys
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from carigma_api.config import Settings  # noqa: E402
from carigma_api.services import emails as mail  # noqa: E402
from carigma_api.services import market, reengagement, triggers  # noqa: E402
from carigma_api.services.repository import emails_by_user_id, service_client  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("send_emails")


#: Ports that speak TLS from the first byte (implicit TLS / SMTPS).
IMPLICIT_TLS_PORTS = frozenset({465})


class SmtpMailer:
    """Hostinger SMTP. One connection reused for the whole pass.

    **Two different TLS handshakes, chosen by port.** This originally only did
    the 587 flow, which would have failed on the first real send against
    Hostinger's 465:

    - **465 (SMTPS)** — TLS is negotiated *before* any SMTP conversation, so
      the client must open an SSL socket. Sending a plaintext `EHLO` here gets
      no usable reply; it hangs until the timeout.
    - **587 (submission)** — plaintext connect, then `STARTTLS` upgrades it.

    Calling `starttls()` on a 465 connection is the classic version of this
    mistake, and it fails as a timeout rather than a clear error — which is
    why the port drives the choice rather than a separate flag someone has to
    remember to set consistently with it.
    """

    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._conn: smtplib.SMTP | None = None

    @property
    def uses_implicit_tls(self) -> bool:
        return self._s.smtp_port in IMPLICIT_TLS_PORTS

    def _connect(self) -> smtplib.SMTP:
        if self._conn is None:
            if self.uses_implicit_tls:
                conn: smtplib.SMTP = smtplib.SMTP_SSL(
                    self._s.smtp_host, self._s.smtp_port, timeout=20
                )
            else:
                conn = smtplib.SMTP(self._s.smtp_host, self._s.smtp_port, timeout=20)
                conn.starttls()
            conn.login(self._s.smtp_user, self._s.smtp_pass)
            self._conn = conn
        return self._conn

    def send(self, email: mail.Email) -> None:
        message = EmailMessage()
        message["From"] = self._s.smtp_from or mail.FROM_ADDRESS
        message["To"] = email.to
        message["Subject"] = email.subject
        # A working unsubscribe is not optional, and one-click matters: an
        # unsubscribe that requires a login is an unsubscribe that becomes a
        # spam report.
        message["List-Unsubscribe"] = f"<https://carigma.in/settings?unsubscribe={email.to}>"
        message.set_content(email.body)
        self._connect().send_message(message)

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.quit()
            except Exception:
                # A failed QUIT after every message is already delivered is
                # not worth failing the run over, but it is worth seeing.
                logger.debug("SMTP quit failed", exc_info=True)
            self._conn = None


class SupabaseSendLog:
    """`email_log` (attempts) + `digest_log` (the duplicate guard).

    Both tables are V1's and already existed — I initially invented new ones
    and the cron failed against the real database. `digest_log` in particular
    already carries `unique (user_id, digest_type, sent_on)`, which IS the
    guard: one of each type per user per period, enforced by the database so
    two racing cron processes cannot both send.

    Two tables on purpose: the log answers "what happened?", the guard answers
    "may I send?". One table doing both would make a failed attempt block its
    own retry.
    """

    def __init__(self, db: Any) -> None:
        self._db = db

    def record(
        self,
        email: mail.Email | None,
        *,
        recipient: str,
        email_type: str,
        status: str,
        detail: str | None,
    ) -> None:
        row = {
            "recipient": recipient,
            "email_type": email_type,
            # V1's `status` vocabulary is sent|failed and its constraint is
            # left untouched; the true V2 outcome goes in `outcome`.
            "status": "failed" if status == "failed" else "sent",
            "outcome": status,
            "subject": (email.subject if email else "")[:300],
            # V1 uses `error` for failures; `detail` (added in V2_002) also
            # carries the reason for a skip, which is not an error.
            "error": (detail or "")[:300] if status == "failed" else None,
            "detail": detail,
        }
        try:
            self._db.table("email_log").insert(row).execute()
            return
        except Exception as exc:
            # Code and migrations deploy at different moments. Before V2_002
            # runs, `detail` does not exist and `status` may still be
            # constrained to sent|failed — so fall back to V1's shape rather
            # than losing the log line entirely. Once the migration lands this
            # branch stops being reached.
            logger.info("email_log full write failed (%s) — retrying V1 shape", exc)

        legacy = {k: v for k, v in row.items() if k not in ("detail", "outcome")}
        # Before V2_002 runs there is nowhere to put the precise outcome, so it
        # goes into `error` as text rather than being lost.
        if status not in ("sent", "failed"):
            legacy["error"] = f"{status}: {detail or ''}".strip()[:300]
        try:
            self._db.table("email_log").insert(legacy).execute()
        except Exception:
            # Logging a send must never abort the send loop.
            logger.exception("email_log write failed for %s", recipient)

    def claim_period(self, user_id: str, email_type: str, period_key: str) -> bool:
        """The duplicate guard. A unique-index violation means someone else
        already claimed it, which is a NO, not an error."""
        try:
            # digest_log's unique index is the guard. An insert that violates
            # it means someone else already claimed this period — a NO, not an
            # error.
            self._db.table("digest_log").insert(
                {"user_id": user_id, "digest_type": email_type, "sent_on": period_key}
            ).execute()
            return True
        except Exception as exc:
            logger.info("period already claimed for %s/%s: %s", user_id, period_key, exc)
            return False


def run_reengagement(*, dry_run: bool, only: str = "") -> mail.RunSummary:
    """The lapsed-user market digest. Its own loop, deliberately.

    It selects from `reengagement_sequences`, not from every profile, and its
    send has a precondition none of the others have: **there must be a live
    curated batch.** Folding it into `run()` behind a flag would put a
    conditional that must never be wrong inside a loop optimised for a
    different job.

    Order of refusals, each before the next:

      1. No live market batch  -> nothing sends AT ALL, to anyone. One check
         for the whole run, because the answer cannot differ per user.
      2. Sequence not due      -> skip.
      3. Preferences           -> `mail.send` refuses; MARKET_DIGEST is
         marketing, so an unsubscribe silences it.
      4. Duplicate guard       -> `digest_log`, same as every other send.

    A user who signed in is closed out here rather than in a separate job: the
    cron is the only thing that reads these rows, so the reset belongs where
    the read happens.
    """
    settings = Settings()
    summary = mail.RunSummary()

    if not dry_run and not (settings.smtp_host and settings.smtp_user):
        logger.warning("SMTP is not configured — forcing --dry-run.")
        dry_run = True

    db = service_client(settings)
    now = datetime.now(UTC)

    # ── 1. Is there anything true to say? ──────────────────────────────────
    # Asked ONCE, before any recipient is considered. `gist_for_email` returns
    # None (not []) when no batch is live, so "no research" cannot be mistaken
    # for "an email with an empty section".
    batches = [market.Batch.from_row(r) for r in _rows(db, "market_digests")]
    gist = market.gist_for_email(batches, now)
    if gist is None:
        logger.info(
            "reengagement: no live market batch — nothing sends. "
            "This is the correct outcome for a week with no curated research."
        )
        return summary

    digest = reengagement.build_digest(gist)
    if digest is None:  # pragma: no cover — gist is non-empty by the check above
        logger.info("reengagement: digest built empty — nothing sends.")
        return summary

    mailer = SmtpMailer(settings)
    log = SupabaseSendLog(db)
    emails = emails_by_user_id(db)
    signed_in = _last_sign_in(db)

    for row in _rows(db, "reengagement_sequences"):
        sequence = reengagement.Sequence.from_row(row)
        if not sequence.is_open:
            continue

        # ── The reset. They came back; stop and keep the row as history. ───
        if not reengagement.has_lapsed(signed_in.get(sequence.user_id), now):
            _update_sequence(db, sequence.id, reengagement.close_on_return(now))
            logger.info("reengagement: %s returned — sequence closed", sequence.user_id)
            continue

        if not reengagement.is_due(sequence, now):
            continue

        recipient = emails.get(sequence.user_id, "")
        if not recipient:
            logger.warning("reengagement: no email for %s — skipped", sequence.user_id)
            continue
        if only and recipient.lower() != only.lower():
            continue

        outcome = mail.send(
            mail.Email(
                to=recipient,
                subject=digest.subject,
                body=digest.body,
                email_type=mail.EmailType.MARKET_DIGEST,
            ),
            mailer=mailer,
            log=log,
            user_id=sequence.user_id,
            recipient=recipient,
            email_type=mail.EmailType.MARKET_DIGEST,
            period_key=mail.weekly_key(now.date()),
            # Weekly cadence, but it still consumes the day: a lapsed digest
            # and a daily brief landing together is two emails, and "more
            # reasons, never more frequency" does not have an exception for
            # the ones we send to people who stopped showing up.
            claim_as=triggers.DAILY_SLOT,
            prefs=_prefs_for_user(db, sequence.user_id),
            dry_run=dry_run,
        )
        summary.record(outcome, recipient)

        # Advance ONLY on a real send. A skip, a duplicate or an unsubscribe
        # must not consume one of the eight — otherwise a user who was
        # unsubscribed the whole time is silently "finished" having received
        # nothing.
        if outcome.status is mail.SendStatus.SENT:
            _update_sequence(db, sequence.id, reengagement.record_send(sequence, now))

    mailer.close()
    logger.info("reengagement: %s", summary.line())
    return summary


def _rows(db: Any, table: str) -> list[dict[str, Any]]:
    try:
        return list(db.table(table).select("*").execute().data or [])
    except Exception:
        logger.exception("could not read %s", table)
        return []


def _update_sequence(db: Any, sequence_id: int, update: dict[str, Any]) -> None:
    try:
        db.table("reengagement_sequences").update(update).eq("id", sequence_id).execute()
    except Exception:
        # Loud: a send that happened without its row advancing would repeat.
        logger.exception("could not advance reengagement sequence %s", sequence_id)


def _last_sign_in(db: Any) -> dict[str, Any]:
    """user_id -> last_sign_in_at, from auth. Empty on failure.

    An empty map means every open sequence looks NOT-lapsed and is closed as
    returned, which stops sending. Failing toward silence is the right
    direction: the alternative is mailing someone who came back.
    """
    try:
        users = db.auth.admin.list_users(page=1, per_page=500) or []
    except Exception:
        logger.exception("could not read sign-in times — no reengagement sends this run")
        return {}
    return {u.id: getattr(u, "last_sign_in_at", None) for u in users}


def run(kind: str, *, dry_run: bool, since_hours: int = 24, only: str = "") -> mail.RunSummary:
    settings = Settings()
    summary = mail.RunSummary()

    # A dry run is FORCED when SMTP is not configured. Attempting real sends
    # against missing credentials would produce a wall of failures that look
    # like a bug rather than a missing setting.
    if not dry_run and not (settings.smtp_host and settings.smtp_user):
        logger.warning("SMTP is not configured — forcing --dry-run.")
        dry_run = True

    db = service_client(settings)
    mailer = SmtpMailer(settings)
    log = SupabaseSendLog(db)

    today = datetime.now(UTC).date()
    period = mail.daily_key(today) if kind == "daily" else mail.weekly_key(today)
    email_type = mail.EmailType.DAILY_BRIEF if kind == "daily" else mail.EmailType.WEEKLY_REVIEW

    recipients = _recipients(db)
    if only:
        # Restrict to ONE address. Not a convenience — a safety rail. Widening
        # the lookback to exercise the positive path would otherwise mail real
        # beta users to prove a code path, which is never an acceptable trade.
        recipients = [r for r in recipients if r["email"].lower() == only.lower()]
        logger.info("--only %s -> %d recipient(s)", only, len(recipients))
        if not recipients:
            logger.error("no recipient matches %s — nothing was sent", only)

    for row in recipients:
        facts = _facts_for(db, row["user_id"], kind, since_hours=since_hours)
        built = (
            mail.build_daily_brief(row["email"], facts, greeting_name=row.get("name", ""))
            if kind == "daily"
            else mail.build_weekly_review(row["email"], facts)
        )
        outcome = mail.send(
            built,
            mailer=mailer,
            log=log,
            user_id=row["user_id"],
            recipient=row["email"],
            email_type=email_type,
            period_key=period,
            prefs=_prefs_for(row),
            dry_run=dry_run,
            # The shared ceiling. The weekly review passes it too: a Sunday
            # that carries both a review and a brief is still two emails.
            claim_as=triggers.DAILY_SLOT,
        )
        summary.record(outcome, row["email"])

    mailer.close()
    logger.info("%s: %s", kind, summary.line())
    for failure in summary.failures:
        logger.error("  %s", failure)
    return summary


def _recipients(db: Any) -> list[dict[str, Any]]:
    """Profiles joined to their auth email.

    `profiles` has NO email column and is keyed by `user_id`, not `id` — both
    of which this function originally got wrong, and both of which only
    surfaced by running the cron against the real database. Email preferences
    live in the existing `preferences` jsonb rather than a new column.
    """
    try:
        rows = db.table("profiles").select("user_id,name,preferences").execute().data or []
    except Exception:
        logger.exception("could not read recipients")
        return []

    emails = emails_by_user_id(db)
    return [
        {
            "user_id": r["user_id"],
            "email": email,
            "name": r.get("name", ""),
            "prefs": (r.get("preferences") or {}).get("email", {}),
        }
        for r in rows
        if (email := emails.get(str(r.get("user_id"))))
    ]


def _prefs_for_user(db: Any, user_id: str) -> mail.Preferences:
    """One user's email preferences, read fresh.

    On a read failure this returns `unsubscribed_all=True` — NOT the permissive
    default. If we cannot tell whether someone opted out, the only safe answer
    is to behave as though they did. The alternative mails a person who said no,
    and an unsubscribe is not a retry.
    """
    try:
        rows = (
            db.table("profiles").select("preferences").eq("user_id", user_id).execute().data or []
        )
    except Exception:
        logger.exception("could not read preferences for %s — treating as opted out", user_id)
        return mail.Preferences(unsubscribed_all=True)
    if not rows:
        logger.warning("no profile row for %s — treating as opted out", user_id)
        return mail.Preferences(unsubscribed_all=True)
    return _prefs_for({"prefs": (rows[0].get("preferences") or {}).get("email", {})})


def _prefs_for(row: dict[str, Any]) -> mail.Preferences:
    prefs = row.get("prefs") or {}
    return mail.Preferences(
        daily_brief=bool(prefs.get("daily_brief", True)),
        weekly_review=bool(prefs.get("weekly_review", True)),
        unsubscribed_all=bool(prefs.get("unsubscribed_all", False)),
    )


def _facts_for(db: Any, user_id: str, kind: str, *, since_hours: int = 24) -> Any:
    """Gather what ACTUALLY happened.

    Returns empty facts on a read failure rather than inventing anything — and
    empty facts mean no email, which is the correct outcome: we do not know
    that something happened, so we do not claim it did.
    """
    if kind == "daily":
        facts = mail.DailyFacts()
        try:
            # `is_new_today` is a DERIVED field in the /jobs/feed RESPONSE
            # (contract §9), not a column. This originally filtered on it as if
            # it were stored, so every read raised, every user was marked
            # "Career Scout unreachable", and the daily brief would have
            # NEVER SENT — while looking like a run of quiet days.
            #
            # The honest-nothing behaviour is what surfaced it: the run
            # reported "sources unavailable" rather than "nothing new", which
            # is exactly the distinction that made a silent failure visible.
            #
            # New = first seen since the last brief. `first_seen` is the real
            # column.
            since = (datetime.now(UTC) - timedelta(hours=since_hours)).isoformat()
            new_jobs = (
                db.table("jobs_feed")
                .select("id")
                .eq("user_id", user_id)
                .eq("status", "active")
                .gte("first_seen", since)
                .execute()
                .data
                or []
            )
            facts.new_matches = len(new_jobs)
        except Exception:
            logger.exception("jobs read failed for %s", user_id)
            # "We couldn't look" — NOT "nothing new". Named so the brief can
            # say so rather than implying an all-clear.
            facts.sources_unavailable = (*facts.sources_unavailable, "Career Scout")
        return facts

    return mail.WeeklyFacts()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=["daily", "weekly", "reengagement", "sweep"])
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be sent. Sends nothing and claims no period.",
    )
    parser.add_argument(
        "--since-hours",
        type=int,
        default=24,
        help="Lookback for 'new' matches. 24 in normal operation; widen only "
        "to backfill or to reproduce a specific day.",
    )
    parser.add_argument(
        "--only",
        default="",
        help="Restrict the run to ONE email address. Use this whenever you "
        "widen --since-hours, so testing cannot mail real users.",
    )
    args = parser.parse_args()

    if args.kind == "sweep":
        # Not an email. It lives here because this is the process that already
        # holds a service-role client and runs on a schedule, and a second
        # cron entry point would be a second thing to forget to deploy.
        from carigma_api.services.sweep import sweep

        report = sweep(service_client(Settings()), dry_run=args.dry_run)
        print(json.dumps(report.as_dict(), indent=2))
        return 0

    if args.kind == "reengagement":
        # Its own runner: different recipient source, and one refusal the other
        # kinds do not have (no live market batch means nothing sends at all).
        summary = run_reengagement(dry_run=args.dry_run, only=args.only)
    else:
        summary = run(args.kind, dry_run=args.dry_run, since_hours=args.since_hours, only=args.only)
    # Non-zero only on a genuine failure. Skips are the system working, and a
    # cron that reports failure on a quiet day trains everyone to ignore it.
    return 1 if summary.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
