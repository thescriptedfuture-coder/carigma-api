"""Prove, with the PUBLIC anon key, what an anonymous stranger can actually do.

Run: `python scripts/verify_rls.py`

Why this exists rather than trusting the Supabase linter or a migration file:
after V2_003 was applied, a probe with the anon key still reported writes
succeeding. A migration that ran is not the same as a database that is closed —
RLS enabled with a permissive policy looks identical to RLS off from the
application's side, and only an actual attempt tells them apart.

**This is a read-and-write probe against the real database.** Every write it
attempts is deleted again if it succeeded, and a write that succeeds is the
finding.

## The table list is DERIVED, not maintained

It used to be a hand-written tuple. It listed ten tables while the migrations
had touched eighteen and the database exposed twenty-eight — so "verify with
the anon key" passed green for weeks without ever looking at `job_applications`
or `post_history`. That is the sixth time in this project a gate has checked
less than it claimed, and the fix has to be structural: a list someone must
remember to update is a list that will be wrong.

So the set comes from PostgREST's own schema document. A new table is probed
the moment it exists — there is no list to forget. The only way to opt out is
`EXEMPT`, which requires a written reason and is itself policed: an exemption
naming a table that no longer exists fails the run, because otherwise it would
silently cover the next table to take that name.

## Two keys, two jobs, never confused

The service key ENUMERATES (PostgREST will not serve the schema doc to anon).
The anon key MEASURES. They are never interchanged, because conflating them is
precisely what produced this project's one false security alarm: a probe run
with an `sb_secret_` key reported every table wide open and was measuring its
own privilege.

## What this CANNOT prove

It measures one boundary: anonymous versus everyone. Since V2_005, every table
answers "no rows, 42501" — and did so BEFORE V2_006 and V2_007 were written.
`USING (true)` for `authenticated` is indistinguishable from
`auth.uid() = user_id` when you are not authenticated at all.

The authenticated boundary — user A reading user B's rows — is measured by
`verify_rls_crossuser.py`. Treat a green run HERE as a regression check.
"""

from __future__ import annotations

import sys
import uuid
from typing import Any

import httpx
from supabase import create_client

from carigma_api.config import Settings, get_settings

#: Tables deliberately NOT probed, each with the reason it is exempt. Anything
#: exposed by PostgREST and absent from here must be measured — that rule is
#: enforced below, so this list is the only place an exemption can hide.
EXEMPT: dict[str, str] = {
    # Genuinely public by design: a logged-out user must be able to report that
    # login is broken. Write-only — anon can INSERT and cannot SELECT, which the
    # probe would flag as an exposure without understanding the intent.
    "feedback": "anon INSERT is intentional (write-only bug reporting)",
}

#: Columns never worth sending: the database supplies them.
_GENERATED = {"id", "created_at", "updated_at"}


def assert_key_is_unprivileged(key: str) -> None:
    """Refuse to run unless the measuring key really is the public one.

    This guard exists because its absence produced a false alarm. The first run
    of this script reported every table readable and writable by "anonymous" —
    and `SUPABASE_ANON_KEY` held an `sb_secret_` key, which bypasses RLS by
    design. The probe was measuring its own privilege and calling it a breach.

    Supabase's key formats are indistinguishable by shape at a glance, and the
    variable NAME is not evidence of anything.
    """
    if key.startswith("sb_secret_"):
        raise SystemExit(
            "REFUSING TO RUN: the configured key is an `sb_secret_` key.\n"
            "A secret key bypasses RLS, so every table would look exposed no\n"
            "matter how RLS is configured. Set the `sb_publishable_` key."
        )
    if key.startswith("eyJ"):
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


def fetch_schema(settings: Settings) -> dict[str, Any]:
    """Every table PostgREST exposes, with its columns. SERVICE KEY — read only.

    Used ONLY to decide what to probe. Nothing measured here is reported; the
    measurement happens with the anon key.
    """
    if not settings.supabase_service_key:
        raise SystemExit(
            "REFUSING TO RUN: SUPABASE_SERVICE_KEY is needed to enumerate the\n"
            "schema. Without it the probe would fall back to a hand-written\n"
            "list, which is the drift this script exists to prevent."
        )
    res = httpx.get(
        f"{settings.supabase_url}/rest/v1/",
        headers={
            "apikey": settings.supabase_service_key,
            "Authorization": f"Bearer {settings.supabase_service_key}",
        },
        timeout=30,
    )
    res.raise_for_status()
    definitions = res.json().get("definitions") or {}
    if not definitions:
        raise SystemExit("REFUSING TO RUN: PostgREST returned no table definitions.")
    return definitions


def probe_row(columns: dict[str, Any], required: list[str]) -> dict[str, Any]:
    """A row complete enough that reaching a constraint MEANS something.

    ## The false inference this replaces

    The old version sent the smallest row it could build — one `user_id`, or
    one text column — reasoning that "a constraint runs only after the RLS
    check passes, so reaching one proves RLS did not stop the write."

    **That is false for NOT NULL.** Column constraints are evaluated while the
    tuple is formed, before the row-level `WITH CHECK` policy is consulted. So
    an under-filled row hits 23502 without RLS ever having an opinion, and the
    probe read that as an exposure.

    It reported `referrals` as anonymously writable. `referrals` has no
    `user_id`, so the fallback sent a single string column; the NOT NULL
    violation on `referrer_id` was scored as "passed RLS". Filling the required
    columns by hand and retrying returns 42501 — refused, as designed.

    A security probe that cries wolf gets ignored, which is worse than not
    having one.
    """
    row: dict[str, Any] = {}
    for name in required:
        if name in _GENERATED:
            continue
        spec = columns.get(name) or {}
        fmt = str(spec.get("format") or "")
        if fmt == "uuid":
            row[name] = str(uuid.uuid4())
        elif fmt.startswith(("integer", "bigint", "smallint", "numeric", "double")):
            row[name] = 1
        elif fmt == "boolean":
            row[name] = False
        elif fmt.startswith("timestamp") or fmt == "date":
            row[name] = "2026-01-01T00:00:00Z"
        elif fmt == "jsonb" or fmt == "json":
            row[name] = {}
        else:
            row[name] = f"rls-probe-{uuid.uuid4()}"
    if row:
        return row

    # Nothing required beyond generated columns. Fall back to any owner column
    # or the first writable text column, so the attempt is still made.
    if "user_id" in columns:
        return {"user_id": str(uuid.uuid4())}
    for name, spec in columns.items():
        if name not in _GENERATED and spec.get("type") == "string":
            return {name: f"rls-probe-{uuid.uuid4()}"}
    return {}


def main() -> int:
    settings = get_settings()
    assert_key_is_unprivileged(settings.supabase_anon_key)

    definitions = fetch_schema(settings)
    exposed = set(definitions)
    to_probe = sorted(exposed - set(EXEMPT))

    client = create_client(settings.supabase_url, settings.supabase_anon_key)

    exposed_read: list[str] = []
    exposed_write: list[str] = []
    unmeasured: list[str] = []

    print(
        f"PostgREST exposes {len(exposed)} tables. Probing {len(to_probe)}, {len(EXEMPT)} exempt.\n"
    )
    print("Probing as ANONYMOUS (public anon key, no session).\n")

    for table in to_probe:
        columns = definitions[table].get("properties") or {}

        # ── Read ────────────────────────────────────────────────────────────
        try:
            res = client.table(table).select("*").limit(1).execute()
            rows = res.data if isinstance(res.data, list) else []
            # A SELECT that succeeds and returns NOTHING is RLS working, not a
            # leak: PostgREST answers 200 with an empty set. Only rows actually
            # handed over count.
            read = f"{len(rows)} row(s) RETURNED" if rows else "no rows (RLS filtered)"
            if rows:
                exposed_read.append(table)
        except Exception as exc:
            read = "read refused"
            if "PGRST205" in str(exc):
                print(f"  {table:20} NOT FOUND")
                continue

        # ── Write ───────────────────────────────────────────────────────────
        row = probe_row(columns, definitions[table].get("required") or [])
        if not row:
            unmeasured.append(table)
            print(f"  {table:20} {read:28} write NOT ATTEMPTED (no usable column)")
            continue

        try:
            client.table(table).insert(row).execute()
        except Exception as exc:
            detail = str(exc)
            if "42501" in detail or "row-level security" in detail:
                write = "REFUSED BY RLS (42501)"
            elif "PGRST204" in detail or "Could not find" in detail:
                # Should be impossible now the row is built from real columns.
                # Counted as unmeasured rather than passed, because it is.
                write = "UNMEASURED - PostgREST rejected the shape"
                unmeasured.append(table)
            elif "23502" in detail:
                # NOT NULL is checked while the tuple is formed, BEFORE the RLS
                # `WITH CHECK` policy. Reaching it proves nothing either way, so
                # it is unmeasured rather than either verdict. This is the false
                # positive that reported `referrals` as anonymously writable.
                write = "UNMEASURED - not-null hit before RLS was consulted"
                unmeasured.append(table)
            elif any(code in detail for code in ("23503", "23505", "22P02")):
                # A foreign key, a unique index and a type cast are all checked
                # after the row-level policy, so reaching one DOES prove the
                # write got past RLS.
                write = "PASSED RLS, stopped by a constraint only"
                exposed_write.append(table)
            else:
                write = f"write failed: {detail[:50]}"
                unmeasured.append(table)
        else:
            write = "WRITE SUCCEEDED"
            exposed_write.append(table)
            col, value = next(iter(row.items()))
            try:
                client.table(table).delete().eq(col, value).execute()
            except Exception:
                print(f"  !! could not clean up probe row in {table} ({col}={value})")

        print(f"  {table:20} {read:28} {write}")

    # ── Coverage ────────────────────────────────────────────────────────────
    #
    # There is deliberately NO "exposed but not covered" check. `to_probe` is
    # derived as `exposed - EXEMPT`, so that set is empty by construction and
    # the check could never fire. A guard that cannot fail is worse than no
    # guard, because it reports success. (It was written that way first and
    # caught by asking what input would make it fail. Nothing would.)
    #
    # Derivation removes the drift the hand-written list had: a new table is
    # probed the moment it exists. What derivation does NOT remove is the
    # escape hatch, so EXEMPT is what gets policed.
    print()
    stale = sorted(set(EXEMPT) - exposed)
    if stale:
        print("EXEMPTION FAILURE — exempt but no longer exposed:")
        for t in stale:
            print(f"  {t}  ({EXEMPT[t]})")
        print(
            "\nAn exemption that outlives its table silently covers the next"
            "\ntable to take that name. Remove it."
        )
        return 1

    print(
        f"Coverage: {len(to_probe)}/{len(exposed)} probed, "
        f"{len(EXEMPT)} exempt ({', '.join(EXEMPT)}). "
        f"Derived from PostgREST — a new table is probed automatically."
    )

    if unmeasured:
        print(f"\nUNMEASURED ({len(unmeasured)}): {', '.join(unmeasured)}")
        print("These printed a line without proving anything. Fix before trusting.")

    print(f"ANON CAN READ  ({len(exposed_read)}): {', '.join(exposed_read) or 'none'}")
    print(f"ANON CAN WRITE ({len(exposed_write)}): {', '.join(exposed_write) or 'none'}")
    print()

    if exposed_write or exposed_read:
        print("VERDICT: EXPOSED.")
        return 1
    if unmeasured:
        print("VERDICT: inconclusive — some tables were not actually measured.")
        return 1
    print("VERDICT: closed to anonymous access.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
