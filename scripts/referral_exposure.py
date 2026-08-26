"""Read-only: has any referral been marked activated without its credits?

`SupabaseReferralStore.activate` flips `referrals.state` to `activated` with a
conditional update, then grants two ledger rows in a loop. A failure between
those leaves the row activated and the credits ungranted — and a retry returns
None immediately, because the state already says activated. The credits are
owed with no path to recovery.

This counts the rows in that condition. It writes nothing.
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

    rows = client.table("referrals").select("*").execute().data or []
    by_state: dict[str, int] = {}
    for row in rows:
        key = str(row.get("state"))
        by_state[key] = by_state.get(key, 0) + 1

    print(f"referrals: {len(rows)} row(s)")
    for state, n in sorted(by_state.items()):
        print(f"  {state:12} {n}")

    activated = [r for r in rows if str(r.get("state")) == "activated"]
    if not activated:
        print("\nNo activated referrals. Exposure to the ungranted-credits window is ZERO.")
        return 0

    ledger = client.table("credit_ledger").select("user_id,kind,delta").execute().data or []
    referral_rows = [entry for entry in ledger if "referral" in str(entry.get("kind", "")).lower()]
    paid = {str(entry.get("user_id")) for entry in referral_rows}

    print(f"\n{len(activated)} activated; {len(referral_rows)} referral ledger row(s)")
    owed = []
    for row in activated:
        for who in ("referrer_id", "referred_id"):
            user = str(row.get(who))
            if user not in paid:
                owed.append((who, user[:8] + "...", str(row.get("activated_at"))[:10]))

    if owed:
        print("\nACTIVATED BUT NOT CREDITED — each of these is someone owed credits:")
        for who, user, when in owed:
            print(f"  {who:12} {user}  activated {when}")
    else:
        print("\nEvery activated referral has ledger rows for both parties. Exposure is ZERO.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
