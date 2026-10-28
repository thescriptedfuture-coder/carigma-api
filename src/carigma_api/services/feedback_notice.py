"""A note from the feedback widget, emailed to support (S15, UAT 28 Oct).

Ravi sent three notes and looked for them at support@carigma.in. They were
stored — the widget stores, and never emailed, in V1 either — and nothing in
V2 could show them. So each note is now stored, THEN emailed: the email is the
alert, the admin list is the record, and an email that does not arrive loses
nothing.

Not a `services.emails.Email`. That type is for mail to users and enforces the
tone law on every word; this one carries a user's own words to us verbatim,
and a note that says "I missed my post" must not fail to send because of it.

`Reply-To` is the sender, so answering a note is pressing Reply.
"""

from __future__ import annotations

import logging
from datetime import datetime
from email.message import EmailMessage
from typing import Any

from carigma_api.config import Settings
from carigma_api.services import smtp
from carigma_api.services.clock import IST

logger = logging.getLogger(__name__)

KIND_LABELS = {"idea": "Idea", "bug": "Bug", "rating": "Rating"}


def notice(
    row: dict[str, Any], *, kind: str, text: str, at: datetime, to: str, sender: str
) -> EmailMessage:
    """The email for one stored note. Pure, so its wording is testable."""
    page = str(row.get("page") or "").split("|")[0] or "—"
    label = KIND_LABELS.get(kind, kind.title())
    message = EmailMessage()
    message["From"] = sender
    message["To"] = to
    message["Subject"] = f"Feedback · {label} · {page}"
    if row.get("email"):
        message["Reply-To"] = str(row["email"])
    lines = [
        f"{label} from {row.get('email') or 'an unknown address'}",
        f"Page: {page}",
    ]
    if row.get("rating"):
        lines.append(f"Rating: {row['rating']} of 5")
    lines += [
        f"Sent: {at.astimezone(IST):%d %b %Y, %H:%M} IST",
        f"User id: {row.get('user_id') or '—'}",
        "",
        text,
        "",
        "Stored in Carigma — every note is in Admin → Feedback.",
    ]
    message.set_content("\n".join(lines))
    return message


def send(settings: Settings, row: dict[str, Any], *, kind: str, text: str, at: datetime) -> None:
    """Email a note that is ALREADY stored. Never raises: by now the note is
    safe, and a failed alert must not turn into an error for the person who
    sent it."""
    if not smtp.configured(settings):
        logger.warning(
            "feedback stored but not emailed: SMTP is not set on this service "
            "(read it in Admin → Feedback)"
        )
        return
    try:
        conn = smtp.connect(settings)
        try:
            conn.send_message(
                notice(
                    row,
                    kind=kind,
                    text=text,
                    at=at,
                    to=settings.feedback_to,
                    sender=settings.smtp_from,
                )
            )
        finally:
            try:
                conn.quit()
            except Exception:
                logger.debug("SMTP quit failed after a feedback notice", exc_info=True)
    except Exception:
        logger.exception(
            "FEEDBACK EMAIL NOT SENT — the note is stored; read it in Admin → Feedback"
        )


__all__ = ["KIND_LABELS", "notice", "send"]
