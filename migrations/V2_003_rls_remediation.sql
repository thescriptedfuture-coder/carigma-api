-- ───────────────────────────────────────────────────────────────────────────
-- V2_003 — RLS remediation for credit_requests and admin_notes
--
-- RUN THIS NOW. Both tables are currently readable AND writable with the
-- public anon key. Verified against the live database on 2026-08-08:
--
--     anon SELECT admin_notes      -> returned a row
--     anon INSERT admin_notes      -> succeeded
--     anon INSERT credit_requests  -> succeeded
--
-- V2_002 created these tables. Its RLS block was added afterwards, and the
-- version that actually ran did not contain it — so the tables exist without
-- protection. This file is separate from V2_002 rather than a re-run of it so
-- there is an unambiguous record of what was applied and when.
--
-- Safe to run more than once: `enable row level security` is idempotent.
--
-- carigma:rls-service-role-only — both tables are admin-only. Every code path
-- reaches them through the SERVICE ROLE, which bypasses RLS, so enabling it
-- with NO policies is the intended end state: anon and authenticated get
-- nothing, admin is unaffected.
-- ───────────────────────────────────────────────────────────────────────────

alter table public.credit_requests enable row level security;
alter table public.admin_notes     enable row level security;


-- ───────────────────────────────────────────────────────────────────────────
-- Verification — run these after, and read the results.
--
-- Expect BOTH to report rowsecurity = true. If either is false the statement
-- above did not apply and the exposure is still open.
-- ───────────────────────────────────────────────────────────────────────────
select
    c.relname                        as table_name,
    c.relrowsecurity                 as rls_enabled,
    (select count(*)
       from pg_policies p
      where p.schemaname = 'public'
        and p.tablename = c.relname) as policy_count
  from pg_class c
  join pg_namespace n on n.oid = c.relnamespace
 where n.nspname = 'public'
   and c.relname in ('credit_requests', 'admin_notes');

-- Expected:
--   credit_requests | true | 0
--   admin_notes     | true | 0
--
-- policy_count = 0 is CORRECT here. RLS with no policy denies anon and
-- authenticated everything, which is exactly what these tables need.
