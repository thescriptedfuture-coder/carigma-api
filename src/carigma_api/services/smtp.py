"""One SMTP connection, opened the way Hostinger needs it.

Moved here from `scripts/send_emails.py` when the API itself began sending
(the feedback notice, S15): two copies of the TLS choice below would be two
chances to get it wrong, and the first draft of the cron's copy did.

**Two different TLS handshakes, chosen by port.**

- **465 (SMTPS)** — TLS is negotiated *before* any SMTP conversation, so the
  client must open an SSL socket. A plaintext `EHLO` here gets no usable
  reply; it hangs until the timeout.
- **587 (submission)** — plaintext connect, then `STARTTLS` upgrades it.

Calling `starttls()` on a 465 connection fails as a timeout rather than a clear
error, which is why the port drives the choice rather than a separate flag.
"""

from __future__ import annotations

import smtplib

from carigma_api.config import Settings

IMPLICIT_TLS_PORTS = frozenset({465})


def configured(settings: Settings) -> bool:
    """Whether this deploy can send at all. Unset is a decision, not an error."""
    return bool(settings.smtp_host and settings.smtp_user and settings.smtp_pass)


def connect(settings: Settings, *, timeout: float = 20) -> smtplib.SMTP:
    """A logged-in connection. Raises what smtplib raises."""
    if settings.smtp_port in IMPLICIT_TLS_PORTS:
        conn: smtplib.SMTP = smtplib.SMTP_SSL(
            settings.smtp_host, settings.smtp_port, timeout=timeout
        )
    else:
        conn = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=timeout)
        conn.starttls()
    conn.login(settings.smtp_user, settings.smtp_pass)
    return conn


__all__ = ["IMPLICIT_TLS_PORTS", "configured", "connect"]
