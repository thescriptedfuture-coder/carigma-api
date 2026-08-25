"""A before/after snapshot for a parity session.

    ./run scripts/parity_baseline.py            # before
    ./run scripts/parity_baseline.py --compare  # after

Writes `parity_baseline.json` beside itself the first time, then diffs against
it. Read-only: it counts rows and reads a handful of fields, and writes nothing
to the database.

## Why counts rather than eyeballing

"Did the ledger move?" answered from memory is not evidence, and the parity
sheet's whole rule is that a claim passes when you have SEEN the thing it
names. This makes the seeing cheap and exact — including the negative claims,
which are the ones a person cannot verify by looking: *nothing* was charged,
*no* extra row appeared.

No field VALUES are printed. Twelve people's headlines on a terminal is a
data-handling decision nobody made.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from carigma_api.config import get_settings  # noqa: E402

HERE = Path(__file__).resolve().parent
SNAPSHOT = HERE / "parity_baseline.json"

#: Tables a session should move, and tables it must NOT.
TABLES = [
    "profiles",
    "credit_ledger",
    "credits",
    "score_history",
    "naukri_scores",
    "agent_runs",
    "email_log",
    "digest_log",
    "referrals",
    "referral_codes",
    "weekly_review",
    "content_loop",
    "jobs_feed",
]


def take(client) -> dict:  # type: ignore[no-untyped-def]
    out: dict = {"counts": {}, "onboarded": 0, "with_platforms": 0}
    for table in TABLES:
        try:
            res = client.table(table).select("*", count="exact").limit(1).execute()
            out["counts"][table] = res.count
        except Exception as exc:
            out["counts"][table] = f"unreadable: {str(exc)[:60]}"

    try:
        rows = client.table("profiles").select("onboarded,platforms").execute().data or []
        out["onboarded"] = sum(1 for r in rows if r.get("onboarded"))
        out["with_platforms"] = sum(1 for r in rows if r.get("platforms"))
    except Exception as exc:
        # Not `pass`. A baseline that silently omits a field would make the
        # comparison afterwards quietly narrower than it looks — which is the
        # failure this whole script exists to make visible.
        print(f"  (could not read profiles for the onboarded/platforms counts: {exc})")
        out["onboarded"] = out["with_platforms"] = "unreadable"
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--compare", action="store_true", help="diff against the saved baseline")
    args = ap.parse_args()

    settings = get_settings()
    if not settings.supabase_service_key:
        print("SUPABASE_SERVICE_KEY is not set; this script only reads, but it needs it.")
        return 2

    from supabase import create_client

    now = take(create_client(settings.supabase_url, settings.supabase_service_key))

    if not args.compare:
        SNAPSHOT.write_text(json.dumps(now, indent=2) + "\n", encoding="utf-8")
        print(f"Baseline written to {SNAPSHOT.name}\n")
        for table, count in now["counts"].items():
            print(f"  {table:18} {count}")
        print(f"\n  onboarded profiles   {now['onboarded']}")
        print(f"  profiles w/ platforms {now['with_platforms']}")
        return 0

    if not SNAPSHOT.exists():
        print("No baseline to compare against. Run without --compare first.")
        return 2

    before = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    print(f"{'table':18} {'before':>8} {'after':>8} {'delta':>8}")
    print("-" * 46)
    for table in TABLES:
        was, is_ = before["counts"].get(table), now["counts"].get(table)
        if isinstance(was, int) and isinstance(is_, int):
            delta = is_ - was
            mark = "  <-- " if delta else ""
            print(f"{table:18} {was:>8} {is_:>8} {delta:>+8}{mark}")
        else:
            print(f"{table:18} {str(was):>8} {str(is_):>8}        ?")

    print()
    print(f"onboarded profiles   {before['onboarded']} -> {now['onboarded']}")
    print(f"profiles w/ platforms {before['with_platforms']} -> {now['with_platforms']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
