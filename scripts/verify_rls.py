"""Prove, with the PUBLIC anon key, what an anonymous stranger can actually do.

Run: `python scripts/verify_rls.py`

Why this exists rather than trusting the Supabase linter or a migration file:
after V2_003 was applied, a probe with the anon key still reported writes
succeeding. A migration that ran is not the same as a database that is closed —
RLS enabled with a permissive policy looks identical to RLS off from the
application's side, and only an actual attempt tells them apart.

**This is a read-and-write probe against the real database.** Every write it
attempts is deleted again if it succeeded, and a write that succeeds is the
finding. It uses the ANON key only: no service key, no user JWT. That is exactly
the position of someone who read the key out of the deployed frontend bundle,
which is where the anon key legitimately lives.
"""

from __future__ import annotations

import sys
import uuid
from typing import Any

from supabase import create_client

from carigma_api.config import get_settings

#: Every table V2 touches. Names come from the migrations, not from memory.
TABLES = (
    "profiles",
    "credits",
    "credit_ledger",
    "score_history",
    "jobs_feed",
    "agent_runs",
    "credit_requests",
    "admin_notes",
    "email_log",
    "digest_log",
)

#: The minimum row each table will accept. Only used to test whether the write
#: is REFUSED — nothing here is meaningful data, and it is removed on success.
PROBE_ROWS: dict[str, dict[str, Any]] = {
    "profiles": {"user_id": None},
    "credits": {"user_id": None, "balance": 0},
    "credit_ledger": {"user_id": None, "delta": 0},
    "score_history": {"user_id": None, "score": 0},
    "jobs_feed": {"user_id": None, "title": "rls-probe"},
    "agent_runs": {"user_id": None, "agent": "rls-probe", "status": "queued"},
    "credit_requests": {"user_id": None, "note": "rls-probe"},
    "admin_notes": {"user_id": None, "body": "rls-probe"},
    "email_log": {"user_id": None, "email_type": "rls-probe", "status": "failed"},
    "digest_log": {"user_id": None, "digest_type": "rls-probe"},
}


def main() -> int:
    settings = get_settings()
    client = create_client(settings.supabase_url, settings.supabase_anon_key)

    exposed_read: list[str] = []
    exposed_write: list[str] = []
    missing: list[str] = []

    print("Probing as ANONYMOUS (public anon key, no session).\n")

    for table in TABLES:
        # ── Read ────────────────────────────────────────────────────────────
        try:
            res = client.table(table).select("*").limit(1).execute()
            rows = res.data if isinstance(res.data, list) else []
            read = f"READ OK ({len(rows)} row visible)"
            exposed_read.append(table)
        except Exception as exc:
            text = str(exc)
            if "does not exist" in text or "PGRST205" in text:
                missing.append(table)
                print(f"  {table:18} TABLE NOT FOUND — migration not applied?")
                continue
            read = "read refused"

        # ── Write ───────────────────────────────────────────────────────────
        row = dict(PROBE_ROWS[table])
        probe_id = str(uuid.uuid4())
        row["user_id"] = probe_id
        try:
            client.table(table).insert(row).execute()
        except Exception as exc:
            detail = str(exc)
            if "row-level security" in detail or "42501" in detail:
                write = "write refused by RLS"
            elif "violates" in detail or "null value" in detail or "invalid input" in detail:
                # Refused by a constraint, NOT by RLS. That is not protection:
                # a better-shaped row would go in. Reported as exposed.
                write = "WRITE REACHED THE TABLE (blocked only by a column constraint)"
                exposed_write.append(table)
            else:
                write = f"write failed: {detail[:70]}"
        else:
            write = "WRITE SUCCEEDED"
            exposed_write.append(table)
            # Put it back the way we found it.
            try:
                client.table(table).delete().eq("user_id", probe_id).execute()
            except Exception:
                print(f"  !! could not clean up probe row in {table} (user_id={probe_id})")

        print(f"  {table:18} {read:28} {write}")

    print()
    if missing:
        print(f"NOT FOUND ({len(missing)}): {', '.join(missing)}")
    print(f"ANON CAN READ ({len(exposed_read)}): {', '.join(exposed_read) or 'none'}")
    print(f"ANON CAN WRITE ({len(exposed_write)}): {', '.join(exposed_write) or 'none'}")
    print()

    if exposed_write:
        print("VERDICT: EXPOSED. An anonymous stranger can write to the tables above.")
        return 1
    if exposed_read:
        print("VERDICT: readable but not writable. Check whether each read is intended.")
        return 1
    print("VERDICT: closed to anonymous access.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
