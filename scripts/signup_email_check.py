"""Read-only: did Supabase ATTEMPT to send a confirmation email?

Signup produced no email. That has two very different causes and they are
distinguishable from the auth table:

- `confirmation_sent_at` is NULL — Supabase never tried. Confirmation is off,
  or the request never reached it.
- `confirmation_sent_at` is set and `email_confirmed_at` is NULL — Supabase
  tried and the mail did not arrive. That is a DELIVERY problem: the project's
  SMTP, or the built-in service, which is rate-limited to a handful an hour and
  drops the rest silently.

Worth being explicit about which system is responsible: **Supabase sends
confirmation emails, not this API.** `SMTP_*` here is the cron's mailer and has
nothing to do with signup. A misread of that would send someone changing the
wrong settings.

No addresses printed — counts, booleans and timestamps only.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from carigma_api.config import get_settings  # noqa: E402


def main() -> int:
    settings = get_settings()
    if not settings.supabase_service_key:
        print("SUPABASE_SERVICE_KEY is not set. This script only reads, but it needs it.")
        return 2

    from supabase import create_client

    client = create_client(settings.supabase_url, settings.supabase_service_key)

    try:
        page = client.auth.admin.list_users(page=1, per_page=100)
    except Exception as exc:
        print(f"could not list auth users: {exc}")
        return 1

    users = list(page or [])
    print(f"auth users: {len(users)}")

    def when(user: object, field: str) -> str:
        value = getattr(user, field, None)
        return str(value)[:19] if value else "—"

    newest = sorted(users, key=lambda u: str(getattr(u, "created_at", "")), reverse=True)[:5]

    print("\nfive most recent, oldest field first:")
    print(f"  {'created':20} {'confirmation_sent':20} {'email_confirmed':20} provider")
    for user in newest:
        identities = getattr(user, "app_metadata", {}) or {}
        provider = identities.get("provider", "?") if isinstance(identities, dict) else "?"
        print(
            f"  {when(user, 'created_at'):20} {when(user, 'confirmation_sent_at'):20} "
            f"{when(user, 'email_confirmed_at'):20} {provider}"
        )

    unconfirmed = [u for u in users if not getattr(u, "email_confirmed_at", None)]
    never_sent = [u for u in unconfirmed if not getattr(u, "confirmation_sent_at", None)]

    print(f"\nunconfirmed accounts: {len(unconfirmed)}")
    print(f"  of those, Supabase never even ATTEMPTED a send: {len(never_sent)}")

    if never_sent:
        print(
            "\n  -> Supabase did not try. Either 'Confirm email' is off (in which case signUp "
            "should have returned a session and the app should not be showing 'check your "
            "email'), or the signup never reached the auth service."
        )
    elif unconfirmed:
        print(
            "\n  -> Supabase TRIED and the mail did not arrive. That is delivery: the project's "
            "custom SMTP, or the built-in service, which is rate-limited to a few an hour and "
            "drops the rest without telling anyone. Dashboard -> Authentication -> Emails."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
