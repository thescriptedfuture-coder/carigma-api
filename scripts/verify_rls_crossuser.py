"""Prove that one signed-in user cannot read another signed-in user's rows.

Run: `python scripts/verify_rls_crossuser.py`

`verify_rls.py` measures the ANONYMOUS boundary, and since V2_005 that boundary
has been closed on every table. It therefore cannot tell `USING (true)` from
`auth.uid() = user_id` — both refuse a caller with no session. Every table looks
identical from out there whether or not V2_006 and V2_007 did anything.

This script measures the boundary that was actually changed.

## Two assertions, and the second is the one people forget

For each owner-scoped table:

  LEAK    signed in as A, filtering on B's user_id, returns 0 rows
  ACCESS  signed in as A, A still sees A's own row

A table locked to NOBODY passes the leak test perfectly. So does a table that
does not exist. Without the access assertion, the strongest possible result of
this script would be "we broke everything", reported as success. That is the
same shape as the cap counter returning 0 for "cannot count" — a failure
wearing a pass.

## Throwaway users, always cleaned up

Two users are created through the Auth admin API with `email_confirm: true`, so
no mail is ever sent to anybody. They are deleted in a `finally`, and the
deletion is CONFIRMED by re-fetching and requiring a 404 — an unverified delete
is a leak of test accounts into a live project.

**Never point this at real beta accounts.** It writes rows as those users.
"""

from __future__ import annotations

import sys
import uuid
from typing import Any

import httpx
from supabase import create_client

from carigma_api.config import Settings, get_settings

#: Substituted per user at seed time with that user's job_applications row.
APP_ID = "<application_id>"

#: Substituted with a value unique to this run AND this user. For columns that
#: are UNIQUE, so two seeded rows cannot collide with each other or with a row
#: a previous run failed to clean up.
SALTED = "<salted>"

#: A fresh uuid per user, for a uuid primary key the API supplies itself.
NEW_UUID = "<uuid>"

#: A code matching `referral_codes_shape_ck` (`^[a-z0-9]{6,12}$`), unique per
#: run and user. SALTED would fail the check constraint and report SEED FAILED.
CODE = "<code>"

#: `interview_preps` and `application_events` both carry a NOT NULL FK to
#: job_applications, so each user needs one of those before they can have
#: either. It is seeded first and tested too — it was never in the eleven
#: permissive tables, so this is the first time its isolation is measured
#: rather than assumed.
PARENT_TABLE = "job_applications"

#: Every table scoped to `auth.uid() = user_id`, with the minimum row that
#: will actually LAND. The row has to be real: the ACCESS half of the test
#: needs something to read back, so a row rejected by a NOT NULL constraint
#: would silently turn this into a leak-only test.
#:
#: Column names verified against the V1 migrations rather than guessed. Four
#: of them were wrong on the first pass — `plan`, `verdict`, `kind` and a
#: missing `dedup_key` — each of which would have failed the seed and printed
#: "SEED FAILED" instead of measuring anything.
OWNER_SCOPED: dict[str, dict[str, Any]] = {
    # V2_007 group B, in the sensitivity order they were migrated in.
    "content_plan": {"week_start": "2026-01-05", "slot_date": "2026-01-07"},
    "interview_preps": {"application_id": APP_ID, "prep": {}},
    "application_events": {"application_id": APP_ID, "event_type": "rls-probe"},
    "content_feedback": {"reason": "rls-probe"},
    # V2_008. RLS from birth rather than retrofitted, so this is here from the
    # table's first day instead of being added after a probe found it missing.
    "profile_updates": {"kind": "skill", "magnitude": "quiet"},
    "jobs_feed": {"dedup_key": "rls-probe", "title": "rls-probe", "company": "rls-probe"},
    "milestones": {"milestone_key": "rls-probe"},
    # V2_011, the four that ended the in-memory stores. Here from their first
    # day, like `profile_updates` — the alternative is adding them after a
    # probe finds them missing, which is a phase of exposure.
    "content_loop": {"week_start": "2026-01-05", "day": "MON", "slot_date": "2026-01-05"},
    "content_weeks": {"week_start": "2026-01-05"},
    "tracker": {"title": "rls-probe", "company": "rls-probe"},
    "weekly_review": {"week_start": "2026-01-05"},
    # V2_006, the financial pair.
    "payments": {"credits": 0, "amount_inr": 0},
    # `sub_id` is NOT NULL and UNIQUE — missed on the first run, which reported
    # SEED FAILED rather than quietly scoring a pass. Salted per user, since two
    # rows are inserted and a shared value would collide.
    "user_subscriptions": {"plan_key": "rls-probe", "sub_id": SALTED},
    # ── Written by the API as the user, and never probed live until 2026-09-17.
    # The RLS measurement found eleven such tables verified only by migration
    # text; these eight were not in this list at all (the V2_011 three were,
    # but the recorded 9/9 run predates them).
    "profiles": {},
    "score_history": {"score": 0},
    "credits": {"balance": 0},
    "credit_ledger": {"delta": 0, "reason": "rls-probe"},
    "naukri_scores": {"score": 0},
    "thread_decisions": {"item_key": SALTED, "decision": "dismissed"},
    "agent_runs": {"id": NEW_UUID, "agent": "rls-probe"},
    "referral_codes": {"code": CODE},
}

#: Every table the API writes through the USER client, and how — the live half
#: of the RLS RETURNING question.
#:
#: supabase-py asks for the written row back on every insert, update and upsert.
#: Postgres then requires the row to pass the table's SELECT policy, and if it
#: does not, the write ERRORS and rolls back — a correct refusal that reaches
#: the user as "we couldn't save that". The code measurement found no table
#: where that can happen; migration text is not the database, so this asks the
#: database, using each table's real write shape:
#:
#: - ("insert", None)          INSERT ... RETURNING
#: - ("upsert", conflict)      INSERT ... ON CONFLICT DO UPDATE ... RETURNING,
#:                             run TWICE so the DO UPDATE path is exercised
#: - ("update", {patch})       UPDATE ... RETURNING on the user's own row,
#:                             which the service role seeds first
#: - ("insert+update", {patch}) both, as the user, when the API does both
#:
#: Only A writes as itself. B is still seeded by the service role, so the
#: leak and access halves keep one side independent of the policy under test.
#:
#: `tests/test_rls_probe.py` holds this list to the code: every table written in
#: `src/` must be here or in its service-only list, with a reason.
WRITES_AS_USER: dict[str, tuple[str, Any]] = {
    "profiles": ("upsert", "user_id"),  # ProfileRepository.save
    "score_history": ("insert", None),  # ScoreRepository.record
    "credits": ("upsert", "user_id"),  # SupabaseCreditStore.apply_delta
    "credit_ledger": ("insert", None),  # SupabaseCreditStore.apply_delta
    "naukri_scores": ("insert", None),  # routes/naukri.py
    "thread_decisions": ("upsert", "user_id,item_key"),  # thread_store
    "agent_runs": ("upsert", "id"),  # run_store
    "referral_codes": ("insert", None),  # referral_store.code_for
    "content_loop": ("upsert", "user_id,week_start,day"),  # posts_store.save_slot
    "content_weeks": ("upsert", "user_id,week_start"),  # posts_store.save_week
    "weekly_review": ("upsert", "user_id,week_start"),  # weekly_store
    "profile_updates": ("insert+update", {"state": "declined"}),  # propose, then decide
    "jobs_feed": ("update", {"status": "dismissed", "dismiss_reason": "rls-probe"}),
    "payments": ("insert+update", {"status": "failed"}),  # record, then claim
    "user_subscriptions": ("update", {"cycles_credited": 0}),  # claim_cycles
}

#: Postgres's code for "new row violates row-level security policy".
RLS_REFUSED = "42501"


def admin_headers(settings: Settings) -> dict[str, str]:
    return {
        "apikey": settings.supabase_service_key,
        "Authorization": f"Bearer {settings.supabase_service_key}",
        "Content-Type": "application/json",
    }


def create_user(settings: Settings, label: str) -> tuple[str, str, str]:
    """A throwaway confirmed user. Returns (id, email, password)."""
    email = f"rls-probe-{label}-{uuid.uuid4().hex[:10]}@carigma-test.invalid"
    password = f"Probe!{uuid.uuid4().hex[:16]}"
    res = httpx.post(
        f"{settings.supabase_url}/auth/v1/admin/users",
        headers=admin_headers(settings),
        json={
            "email": email,
            "password": password,
            # No mail is sent. A probe must never put anything in a real inbox.
            "email_confirm": True,
        },
        timeout=30,
    )
    res.raise_for_status()
    return res.json()["id"], email, password


def delete_user(settings: Settings, user_id: str) -> str:
    """Delete and CONFIRM. Returns a human-readable outcome."""
    httpx.delete(
        f"{settings.supabase_url}/auth/v1/admin/users/{user_id}",
        headers=admin_headers(settings),
        timeout=30,
    )
    check = httpx.get(
        f"{settings.supabase_url}/auth/v1/admin/users/{user_id}",
        headers=admin_headers(settings),
        timeout=30,
    )
    if check.status_code == 404:
        return f"deleted (404 confirmed) {user_id}"
    return f"!! STILL PRESENT after delete: {user_id} (status {check.status_code})"


def signed_in_client(settings: Settings, email: str, password: str) -> Any:
    """A client acting as this user — the publishable key plus a real session.

    Deliberately NOT the service key: the whole question is what a signed-in
    user can reach, and the service role would bypass the answer.
    """
    client = create_client(settings.supabase_url, settings.supabase_anon_key)
    client.auth.sign_in_with_password({"email": email, "password": password})
    return client


def main() -> int:  # noqa: C901 — a linear script reads better than split halves
    settings = get_settings()
    if settings.supabase_anon_key.startswith("sb_secret_"):
        raise SystemExit(
            "REFUSING TO RUN: SUPABASE_ANON_KEY holds a secret key. The sessions\n"
            "would bypass RLS and every table would pass."
        )
    if not settings.supabase_service_key:
        raise SystemExit("REFUSING TO RUN: SUPABASE_SERVICE_KEY is needed to make test users.")

    a_id = b_id = None
    failures: list[str] = []
    checked = 0

    try:
        a_id, a_email, a_pw = create_user(settings, "a")
        b_id, b_email, b_pw = create_user(settings, "b")
        print(f"Throwaway users created (no mail sent).\n  A {a_id}\n  B {b_id}\n")

        a = signed_in_client(settings, a_email, a_pw)
        b = signed_in_client(settings, b_email, b_pw)

        # Service role seeds B's rows: B's OWN client would work too, but using
        # the service role keeps the seeding independent of the policy being
        # tested. If the policy is broken, seeding must not break with it.
        svc = create_client(settings.supabase_url, settings.supabase_service_key)

        # The FK parent, one per user, seeded before anything referencing it.
        # `milestone_key` is unique per user, so it is salted to keep reruns
        # from colliding with a row a previous run failed to clean up.
        salt = uuid.uuid4().hex[:8]
        app_id: dict[str, str] = {}
        for uid in (a_id, b_id):
            created = svc.table(PARENT_TABLE).insert({"user_id": uid}).execute().data
            app_id[uid] = created[0]["id"]

        tables = {PARENT_TABLE: {}, **OWNER_SCOPED}

        print(f"{'table':22} {'sees own':>10}  {'sees other':>12}  {'A writes own':>12}   verdict")
        print("-" * 80)

        def row_for(template: dict[str, Any], uid: str) -> dict[str, Any]:
            out: dict[str, Any] = {}
            for key, value in template.items():
                if value == APP_ID:
                    out[key] = app_id[uid]
                elif value == SALTED:
                    out[key] = f"rls-probe-{salt}-{uid[:8]}"
                elif value == NEW_UUID:
                    out[key] = str(uuid.uuid4())
                elif value == CODE:
                    out[key] = f"{salt}{uid.replace('-', '')[:4]}"
                else:
                    out[key] = value
            if "milestone_key" in out:
                # UNIQUE (user_id, milestone_key), so the salt only needs to
                # separate runs, not users.
                out["milestone_key"] = f"rls-probe-{salt}"
            return {**out, "user_id": uid}

        def read(tbl: str, client: Any, owner: str, who: str, direction: str) -> list[Any]:
            """Both helpers take their loop values as ARGUMENTS rather than
            closing over them. A closure over a loop variable reads the LAST
            iteration's value, which here would silently make every row report
            the final table's result."""
            try:
                return client.table(tbl).select("*").eq("user_id", owner).execute().data or []
            except Exception as exc:
                failures.append(f"{tbl}: {who}'s {direction} errored ({str(exc)[:60]})")
                return []

        def write_as_owner(tbl: str, client: Any, row: dict[str, Any]) -> str:
            """Write A's row AS A, the way the API does. Returns the outcome.

            `ok`, or a label that says which failure it was — because "RLS
            refused a correct write" and "the probe's row was malformed" need
            opposite responses, and one FAIL label would hide which.
            """
            method, arg = WRITES_AS_USER[tbl]
            attempts: list[Any] = []
            try:
                if method == "insert":
                    attempts.append(client.table(tbl).insert(row).execute())
                elif method == "upsert":
                    # Twice: the first may insert, the second MUST take the
                    # ON CONFLICT DO UPDATE path, which checks the existing
                    # row against SELECT and UPDATE policies as well.
                    for _ in range(2):
                        attempts.append(client.table(tbl).upsert(row, on_conflict=arg).execute())
                else:
                    if method == "insert+update":
                        attempts.append(client.table(tbl).insert(row).execute())
                    else:
                        # The API only updates this table; its rows come from
                        # a cron. Seeded by the service role, separately, so a
                        # bad seed row cannot be reported as an RLS finding.
                        try:
                            svc.table(tbl).insert(row).execute()
                        except Exception as exc:
                            return f"SEED FAILED ({str(exc)[:70]})"
                    attempts.append(
                        client.table(tbl).update(arg).eq("user_id", row["user_id"]).execute()
                    )
            except Exception as exc:
                code = str(getattr(exc, "code", "") or "")
                if code == RLS_REFUSED:
                    return f"RLS REFUSED ({str(exc)[:70]})"
                return f"WRITE ERROR, not RLS [{code or type(exc).__name__}] ({str(exc)[:70]})"
            if not all(getattr(res, "data", None) for res in attempts):
                # Postgres errors rather than hiding an inserted row, but an
                # UPDATE whose USING filters the row out returns nothing.
                return "NOTHING RETURNED"
            return "ok"

        writes_checked = 0
        for table, template in tables.items():
            # PARENT_TABLE is already seeded — it had to exist before the rows
            # that reference it.
            wrote = "-"
            if table in WRITES_AS_USER:
                wrote = write_as_owner(table, a, row_for(template, a_id))
                writes_checked += 1
                if wrote != "ok":
                    failures.append(f"{table}: A writing A's own row -> {wrote}")
                try:
                    svc.table(table).insert(row_for(template, b_id)).execute()
                except Exception as exc:
                    failures.append(f"{table}: could not seed B's row ({str(exc)[:90]})")
                    print(f"{table:22} {'-':>10}  {'-':>12}  {wrote[:12]:>12}   SEED FAILED")
                    continue
            elif table != PARENT_TABLE:
                try:
                    svc.table(table).insert(
                        [row_for(template, a_id), row_for(template, b_id)]
                    ).execute()
                except Exception as exc:
                    failures.append(f"{table}: could not seed rows ({str(exc)[:90]})")
                    print(f"{table:22} {'-':>10}  {'-':>12}  {'-':>12}   SEED FAILED")
                    continue

            # LEAK, both directions. Checking only A->B would miss a policy
            # accidentally pinned to one particular user rather than to
            # auth.uid() — unlikely, but the second call is free.
            leaked = read(table, a, b_id, "A", "cross-read") + read(
                table, b, a_id, "B", "cross-read"
            )

            # ACCESS: each still sees their own. Without this, a table locked
            # to NOBODY scores a perfect pass on the leak test — and so does a
            # table that does not exist.
            own_a = read(table, a, a_id, "A", "own-read")
            own_b = read(table, b, b_id, "B", "own-read")

            checked += 1
            ok_leak = not leaked
            ok_own = bool(own_a) and bool(own_b)
            if not ok_leak:
                failures.append(f"{table}: LEAK — {len(leaked)} row(s) read across users")
            if not ok_own:
                missing = "A" if not own_a else "B"
                failures.append(f"{table}: LOCKED OUT — {missing} cannot see their own rows")

            ok_write = wrote in ("ok", "-")
            verdict = "ok" if (ok_leak and ok_own and ok_write) else "FAIL"
            print(
                f"{table:22} {('both' if ok_own else 'NO'):>10}  "
                f"{('no' if ok_leak else f'{len(leaked)} ROWS'):>12}  "
                f"{wrote[:12]:>12}   {verdict}"
            )

    finally:
        print("\nCleanup:")
        for uid in (a_id, b_id):
            if uid:
                print(f"  {delete_user(settings, uid)}")
        # Their rows go with them: every table above is
        # `references auth.users (id) on delete cascade`.

    print()
    if failures:
        print(f"VERDICT: {len(failures)} PROBLEM(S)")
        for f in failures:
            print(f"  - {f}")
        return 1
    # Against the tables actually attempted, which is OWNER_SCOPED plus the FK
    # parent. Compared against len(OWNER_SCOPED) at first, so a fully passing
    # run reported "inconclusive — only 9/8". Wrong, but wrong in the direction
    # that withholds a pass rather than inventing one.
    expected = len(OWNER_SCOPED) + 1
    if checked != expected:
        print(f"VERDICT: inconclusive — only {checked}/{expected} tables measured.")
        return 1
    if writes_checked != len(WRITES_AS_USER):
        print(
            f"VERDICT: inconclusive — only {writes_checked}/{len(WRITES_AS_USER)} "
            "user-written tables were written as their owner."
        )
        return 1
    print(f"VERDICT: all {checked} owner-scoped tables isolate correctly.")
    print("A sees only A. A still sees A.")
    print(
        f"And A can write A's own row, and get it back, on all {writes_checked} "
        "tables the API writes as the user."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
