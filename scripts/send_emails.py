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
import logging
import smtplib
import sys
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from carigma_api.config import Settings  # noqa: E402
from carigma_api.services import emails as mail  # noqa: E402
from carigma_api.services.repository import emails_by_user_id, service_client  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("send_emails")


class SmtpMailer:
    """Hostinger SMTP. One connection reused for the whole pass."""

    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._conn: smtplib.SMTP | None = None

    def _connect(self) -> smtplib.SMTP:
        if self._conn is None:
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


def run(kind: str, *, dry_run: bool) -> mail.RunSummary:
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

    for row in _recipients(db):
        facts = _facts_for(db, row["user_id"], kind)
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


def _prefs_for(row: dict[str, Any]) -> mail.Preferences:
    prefs = row.get("prefs") or {}
    return mail.Preferences(
        daily_brief=bool(prefs.get("daily_brief", True)),
        weekly_review=bool(prefs.get("weekly_review", True)),
        unsubscribed_all=bool(prefs.get("unsubscribed_all", False)),
    )


def _facts_for(db: Any, user_id: str, kind: str) -> Any:
    """Gather what ACTUALLY happened.

    Returns empty facts on a read failure rather than inventing anything — and
    empty facts mean no email, which is the correct outcome: we do not know
    that something happened, so we do not claim it did.
    """
    if kind == "daily":
        facts = mail.DailyFacts()
        try:
            new_jobs = (
                db.table("jobs_feed")
                .select("id")
                .eq("user_id", user_id)
                .eq("is_new_today", True)
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
    parser.add_argument("kind", choices=["daily", "weekly"])
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be sent. Sends nothing and claims no period.",
    )
    args = parser.parse_args()

    summary = run(args.kind, dry_run=args.dry_run)
    # Non-zero only on a genuine failure. Skips are the system working, and a
    # cron that reports failure on a quiet day trains everyone to ignore it.
    return 1 if summary.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
