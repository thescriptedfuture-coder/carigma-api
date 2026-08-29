"""Read-only: is the live cron respecting the audience gate?

The gate is the only thing between `send_emails.py` and twelve live V1 users
who never signed up for V2 mail, and `--dry-run` came off today. This reports
whether anything was sent to an address outside the allow-list.

**Booleans and counts only.** Twelve people's addresses on a terminal is a
data-handling decision nobody made.
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from carigma_api.config import get_settings  # noqa: E402
from carigma_api.services.emails import Audience  # noqa: E402


def main() -> int:
    settings = get_settings()
    if not settings.supabase_service_key:
        print("SUPABASE_SERVICE_KEY is not set. This script only reads, but it needs it.")
        return 2

    from supabase import create_client

    client = create_client(settings.supabase_url, settings.supabase_service_key)
    audience = Audience.from_settings(settings.email_audience, settings.email_allowlist)
    print(f"configured audience: {audience.describe}")

    rows = client.table("email_log").select("*").order("created_at", desc=True).limit(80).execute()
    all_rows = rows.data or []
    if not all_rows:
        print("no email_log rows at all — nothing to check")
        return 0

    newest_day = str(all_rows[0].get("created_at", ""))[:10]
    today = [r for r in all_rows if str(r.get("created_at", ""))[:10] == newest_day]

    print(f"\nmost recent run: {newest_day} ({len(today)} rows)")
    print(f"  outcomes: {dict(Counter(str(r.get('outcome')) for r in today))}")

    sent = [r for r in today if str(r.get("outcome")) == "sent"]
    outside = [r for r in sent if not audience.admits(str(r.get("recipient") or ""))]

    print(f"\n  sent: {len(sent)}")
    print(f"  of those, on the allow-list: {len(sent) - len(outside)}")
    print(f"  OUTSIDE the allow-list: {len(outside)}")

    if outside:
        print("\n  *** The gate did not hold. Investigate before the next cron run. ***")
        return 1

    # A gate nothing reached has not been exercised. Say so rather than
    # reporting "clean", which reads as "it worked".
    refused = [r for r in today if str(r.get("outcome")) == "not_in_audience"]
    if not refused:
        print(
            "\n  No `not_in_audience` rows: nobody outside the allow-list had anything to say, "
            "so the gate was never REACHED. `send()` records a skip before it consults the "
            "audience, so a quiet day exercises none of it. Not evidence that it works."
        )
    else:
        print(f"\n  refused by the gate: {len(refused)} — the gate is doing real work")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
