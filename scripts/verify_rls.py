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

## What this CANNOT prove

It measures one boundary: anonymous versus everyone. Since V2_005 removed
`anon` from the last permissive policies, every table here answers "no rows,
42501" — and it did so BEFORE V2_006 and V2_007 were written.

So a clean run here does not mean the database is correctly scoped. The
remaining risk after V2_005 is entirely on the *authenticated* boundary: a
signed-in user reading another signed-in user's rows. `USING (true)` for
`authenticated` looks identical to `auth.uid() = user_id` from out here,
because both refuse anonymous callers.

Proving that boundary needs two real sessions and a cross-read attempt:

    sign in as A -> select from content_plan where user_id = <B's id>
    expect 0 rows, not B's drafts

Until something does that, V2_006 and V2_007 are verified by their pg_policies
output, not by measurement. Treat a green run here as a REGRESSION check.
"""

from __future__ import annotations

import sys
import uuid
from typing import Any

from supabase import create_client

from carigma_api.config import get_settings

#: Every table the V2 migrations touch. Names come from the migrations, not
#: from memory. This list covered only ten until V2_007 — which meant "verify
#: with the anon key after each step" was checking barely half of what the
#: migrations changed. A probe that silently skips tables is worse than a short
#: one, because the summary line reads the same either way.
TABLES = (
    # V2_001–V2_003: already owner-scoped or service-role-only
    "profiles",
    "credits",
    "credit_ledger",
    "score_history",
    "agent_runs",
    "credit_requests",
    "admin_notes",
    # V2_006: owner-scoped (financial)
    "payments",
    "user_subscriptions",
    # V2_007 group B: owner-scoped
    "content_plan",
    "interview_preps",
    "application_events",
    "content_feedback",
    "jobs_feed",
    "milestones",
    # V2_007 group A: RLS on, NO policies (service-role only)
    "email_log",
    "digest_log",
    "jobs_cache",
)

#: The column identifying a row's owner. `jobs_cache` deliberately has none —
#: it is keyed on the query, so sending a user_id would make PostgREST reject
#: the shape from its schema cache and leave the table unmeasured.
OWNER_COLUMN = "user_id"

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
    # Columns per V2_002. An invented column name makes PostgREST reject the
    # request from its schema cache before it ever reaches Postgres, which
    # leaves the table UNMEASURED while looking like a result.
    "admin_notes": {"user_id": None, "note": "rls-probe", "author": "rls-probe"},
    "email_log": {"user_id": None, "email_type": "rls-probe", "status": "failed"},
    "digest_log": {"user_id": None, "digest_type": "rls-probe"},
    "payments": {"user_id": None, "credits": 0, "amount_inr": 0},
    "user_subscriptions": {"user_id": None, "sub_id": "rls-probe", "plan_key": "rls-probe"},
    "content_plan": {"user_id": None},
    "interview_preps": {"user_id": None},
    "application_events": {"user_id": None},
    "content_feedback": {"user_id": None},
    "milestones": {"user_id": None},
    # No user_id column. Shared infrastructure keyed on the query; its row count
    # for today IS the global JSearch cap counter.
    "jobs_cache": {"query_key": "rls-probe", "source": "rls-probe"},
}


def assert_key_is_unprivileged(key: str) -> None:
    """Refuse to run unless the key really is the public one.

    This guard exists because its absence produced a false alarm. The first run
    of this script reported every table readable and writable by "anonymous" —
    and the key in `SUPABASE_ANON_KEY` was an `sb_secret_` key. A secret key
    bypasses RLS by design, so the probe was measuring its own privilege and
    calling it an exposure.

    Supabase's key formats are indistinguishable by shape at a glance, and the
    variable NAME is not evidence of anything. A probe whose whole output
    depends on running unprivileged must prove it is unprivileged first.
    """
    if key.startswith("sb_secret_"):
        raise SystemExit(
            "REFUSING TO RUN: the configured key is an `sb_secret_` key.\n"
            "A secret key bypasses RLS, so every table would look exposed no\n"
            "matter how RLS is configured. Set the `sb_publishable_` key."
        )
    if key.startswith("eyJ"):
        # A legacy JWT: decode and check the role claim rather than trusting it.
        import base64
        import json

        try:
            body = key.split(".")[1]
            role = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))).get("role")
        except Exception:
            raise SystemExit("REFUSING TO RUN: key looks like a JWT but will not decode.") from None
        if role != "anon":
            raise SystemExit(f"REFUSING TO RUN: key carries role={role!r}, not 'anon'.")
        return
    if not key.startswith("sb_publishable_"):
        raise SystemExit(
            f"REFUSING TO RUN: unrecognised key format (starts {key[:4]!r}).\n"
            "Expected `sb_publishable_` or a legacy anon JWT."
        )


def main() -> int:
    settings = get_settings()
    # Preconditions before measurement. Everything below is only meaningful if
    # this call passes.
    assert_key_is_unprivileged(settings.supabase_anon_key)
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
            # A SELECT that succeeds and returns NOTHING is RLS working, not a
            # leak — PostgREST answers 200 with an empty set rather than an
            # error. Only rows actually handed over count as exposure.
            read = f"{len(rows)} row(s) returned" if rows else "no rows (RLS filtered)"
            if rows:
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
        # Only tables that HAVE an owner column get one. jobs_cache does not,
        # and adding it would make PostgREST reject the shape before Postgres
        # sees it — leaving the table unmeasured while printing a result.
        cleanup_col = OWNER_COLUMN if OWNER_COLUMN in row else "query_key"
        if OWNER_COLUMN in row:
            row[OWNER_COLUMN] = probe_id
        else:
            row[cleanup_col] = f"rls-probe-{probe_id}"
        try:
            client.table(table).insert(row).execute()
        except Exception as exc:
            detail = str(exc)
            if "42501" in detail or "row-level security" in detail:
                # The ONLY response that proves RLS is switched on and enforcing.
                write = "REFUSED BY RLS (42501)"
            elif "PGRST204" in detail or "Could not find" in detail:
                # PostgREST rejected the shape from its schema cache without ever
                # reaching Postgres. This tells us NOTHING about RLS.
                # ASCII only: this prints to a Windows console under cp1252.
                write = "inconclusive - PostgREST rejected the shape, never hit the DB"
            elif "23503" in detail or "23502" in detail or "violates" in detail:
                # Reached the table and was rejected by a CONSTRAINT. A constraint
                # runs only after the RLS check passes, so this proves RLS did not
                # stop the write — a better-shaped row would land.
                write = "PASSED RLS, stopped by a constraint only"
                exposed_write.append(table)
            else:
                write = f"write failed: {detail[:60]}"
        else:
            write = "WRITE SUCCEEDED"
            exposed_write.append(table)
            # Put it back the way we found it.
            try:
                client.table(table).delete().eq(cleanup_col, row[cleanup_col]).execute()
            except Exception:
                print(
                    f"  !! could not clean up probe row in {table} "
                    f"({cleanup_col}={row[cleanup_col]})"
                )

        print(f"  {table:18} {read:28} {write}")

    print()
    print("How to read the write column:")
    print("  42501            = RLS is ON and enforcing. The only proof of that.")
    print("  constraint error = RLS did NOT stop the write. Constraints are checked")
    print("                     AFTER the RLS check, so reaching one means the row")
    print("                     passed. For a table whose only policies are")
    print("                     `auth.uid() = user_id`, this means RLS is DISABLED:")
    print("                     Postgres stores policies whether or not RLS is on,")
    print("                     and enforces them only when it is.")
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
