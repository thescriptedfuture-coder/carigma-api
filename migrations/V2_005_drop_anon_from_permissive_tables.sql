-- ───────────────────────────────────────────────────────────────────────────
-- V2_005 — take `anon` off the eleven permissive V1 tables
--
-- These eleven each carry a policy granting {anon, authenticated} ALL with
-- USING (true). `anon` is the role every holder of the PUBLISHABLE key gets,
-- and that key ships in the browser. So today they are world-readable and
-- world-writable. Measured with the real publishable key on 2026-08-11:
--
--     jobs_feed   READ OK (1 row visible)   PASSED RLS, stopped by a constraint only
--     email_log   READ OK (1 row visible)   PASSED RLS, stopped by a constraint only
--     digest_log  READ OK (1 row visible)   PASSED RLS, stopped by a constraint only
--
-- (Those are the three of the eleven that `scripts/verify_rls.py` covers. The
-- other eight carry the identical policy shape per the V2_004 audit.)
--
-- Contrast with the six tables that are already correct, same run:
--
--     profiles / credits / credit_ledger / score_history / agent_runs /
--     credit_requests   READ OK (0 rows)   REFUSED BY RLS (42501)
--
-- ## What this migration does NOT do
--
-- It keeps `authenticated` with USING (true). That is still far too broad —
-- any signed-in user can read any other user's rows — and tightening it to
-- auth.uid() = user_id is the eventual goal. It is deliberately NOT done here:
--
--   Live V1 runs its user-facing client with the SERVICE-ROLE key (confirmed on
--   Render, 2026-08-11), so RLS is bypassed for every V1 user operation. Until
--   that key is swapped to the publishable one AND the session-isolation fix is
--   deployed, tightening to auth.uid() would change nothing for V1 and would
--   only make the eventual swap riskier to reason about.
--
-- Dropping `anon` is safe today because nothing V1 does reaches these tables
-- unauthenticated. Verified: the auth gate at app.py:1824 calls st.stop()
-- before any DB work, signup touches only auth (no table writes), and the
-- digest cron uses get_supabase_admin() (service role).
--
-- ## Idempotent
--
-- Each policy is dropped and recreated. Re-running is a no-op in effect.
-- ───────────────────────────────────────────────────────────────────────────

-- ## Why this is dynamic SQL, and what that costs
--
-- Rebuilding a policy requires its NAME, and the V2_004 audit output is not in
-- this repo — so the eleven cannot be written out literally without guessing
-- names. The loop reads each policy from pg_policies and recreates it with the
-- same name, command and expressions, changing only the role list.
--
-- The drop uses `drop policy IF EXISTS`, which is the idiom
-- `tests/test_migrations.py` sanctions. That matters more than it looks: the
-- guard now forbids a bare `drop policy` outright, because a RESTRICTIVE policy
-- is AND-ed and dropping one LOOSENS access — and nothing in the statement
-- distinguishes restrictive from permissive. The guard still cannot read inside
-- `execute format(...)` well enough to confirm a `create` follows every `drop`.
--
-- Three things cover the part the guard cannot, and none of them is "it looked
-- fine":
--
--   1. `drop policy` fails CLOSED. Removing a permissive policy from an
--      RLS-enabled table denies more, never less. The failure mode is V1
--      breaking loudly, not data quietly opening.
--   2. A `do $$ ... $$` block is a single statement and therefore atomic. If
--      the create fails after the drop, the whole block rolls back and the
--      policy is still there.
--   3. The result is verified from outside by `scripts/verify_rls.py` with the
--      publishable key, which does not care how the policy got there.

do $$
declare
    t text;
    p record;
    tables text[] := array[
        'application_events',
        'content_feedback',
        'content_plan',
        'digest_log',
        'email_log',
        'interview_preps',
        'jobs_cache',
        'jobs_feed',
        'milestones',
        'payments',
        'user_subscriptions'
    ];
begin
    foreach t in array tables loop
        -- Guard: a table that does not exist means this migration is being run
        -- against the wrong database. Fail loudly rather than silently skipping.
        if not exists (
            select 1 from pg_class c
              join pg_namespace n on n.oid = c.relnamespace
             where n.nspname = 'public' and c.relname = t
        ) then
            raise exception 'V2_005: table public.% does not exist', t;
        end if;

        -- Rebuild every policy on this table that currently includes `anon`,
        -- preserving its name, command, and expressions — only the role list
        -- changes. Reusing the name keeps the audit output comparable.
        for p in
            select policyname, cmd, qual, with_check, roles
              from pg_policies
             where schemaname = 'public'
               and tablename = t
               and 'anon' = any (roles)
        loop
            execute format('drop policy if exists %I on public.%I', p.policyname, t);
            execute format(
                'create policy %I on public.%I as permissive for %s to authenticated%s%s',
                p.policyname, t,
                case p.cmd
                    when 'ALL'    then 'all'
                    when 'SELECT' then 'select'
                    when 'INSERT' then 'insert'
                    when 'UPDATE' then 'update'
                    when 'DELETE' then 'delete'
                end,
                case when p.qual       is not null then ' using (' || p.qual || ')' else '' end,
                case when p.with_check is not null then ' with check (' || p.with_check || ')' else '' end
            );
            raise notice 'V2_005: % on % -> authenticated only', p.policyname, t;
        end loop;
    end loop;
end $$;


-- ───────────────────────────────────────────────────────────────────────────
-- Verification — run this after, and read the result.
--
-- Expect ZERO rows. Any row returned is a policy that still admits `anon`.
-- ───────────────────────────────────────────────────────────────────────────
select tablename, policyname, cmd, roles
  from pg_policies
 where schemaname = 'public'
   and 'anon' = any (roles)
   and tablename in (
       'application_events', 'content_feedback', 'content_plan', 'digest_log',
       'email_log', 'interview_preps', 'jobs_cache', 'jobs_feed', 'milestones',
       'payments', 'user_subscriptions'
   );

-- Deliberately NOT touched, and each for a reason:
--
--   feedback        anon INSERT is intentional — write-only by design, so a
--                   logged-out user can still report that login is broken.
--   market_digests  authenticated + published-only. Correct as it stands.
--   admin_notes     RLS on, no policies. Correct: service-role only.
--   credit_requests same.
--
-- Then run, from carigma-api:  python scripts/verify_rls.py
-- Expect jobs_feed, email_log and digest_log to flip from
-- "READ OK (1 row visible)" to "READ OK (0 row visible)" and from
-- "PASSED RLS" to "REFUSED BY RLS (42501)".
