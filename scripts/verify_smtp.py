"""Send ONE real email, to ourselves, to prove the SMTP path works.

    python scripts/verify_smtp.py

Why this exists separately from `send_emails.py`: running the cron live proves
nothing about SMTP when every user correctly skips (nothing to report). The
only way to exercise the credentials and the Hostinger connection is to send
something on purpose.

**It sends to `SMTP_USER` — our own mailbox — and to nobody else.** No beta
user receives anything from this script. That is deliberate: proving the
transport should never mean mailing real people.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from send_emails import SmtpMailer  # noqa: E402

from carigma_api.config import Settings  # noqa: E402
from carigma_api.services import emails as mail  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("verify_smtp")


def main() -> int:
    s = Settings()

    # Preconditions first. A "success" from an unconfigured mailer would be
    # meaningless, and this is the one script whose whole job is proof.
    missing = [
        name
        for name, value in (
            ("SMTP_HOST", s.smtp_host),
            ("SMTP_USER", s.smtp_user),
            ("SMTP_PASS", s.smtp_pass),
        )
        if not value
    ]
    if missing:
        logger.error("Not configured: %s. Nothing was sent.", ", ".join(missing))
        return 1

    mailer = SmtpMailer(s)
    logger.info(
        "host=%s port=%s tls=%s from=%s",
        s.smtp_host,
        s.smtp_port,
        "implicit (SMTPS)" if mailer.uses_implicit_tls else "STARTTLS",
        s.smtp_from,
    )

    # A real Email object, so the tone-law guard and the whole construction
    # path are exercised — not a hand-rolled MIME message that bypasses them.
    probe = mail.Email(
        to=s.smtp_user,
        subject="Carigma SMTP check — 465 implicit TLS",
        body=(
            "This is a transport check sent to ourselves.\n\n"
            f"host: {s.smtp_host}\n"
            f"port: {s.smtp_port} "
            f"({'implicit TLS' if mailer.uses_implicit_tls else 'STARTTLS'})\n"
            f"from: {s.smtp_from}\n\n"
            "If this arrived, the credentials and the handshake are correct.\n"
        ),
        email_type=mail.EmailType.WELCOME,
    )

    try:
        mailer.send(probe)
    except Exception as exc:
        logger.error("SEND FAILED: %s: %s", type(exc).__name__, exc)
        return 1
    finally:
        mailer.close()

    logger.info("SENT to %s — check that mailbox.", s.smtp_user)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
