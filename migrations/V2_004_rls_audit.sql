-- ───────────────────────────────────────────────────────────────────────────
-- V2_004 — RLS AUDIT (read-only). Run this and read the output.
--
-- WHY: with the PUBLIC anon key — the one shipped in the browser bundle — I
-- can currently read every row of:
--
--     profiles · credits · credit_ledger · score_history · jobs_feed · agent_runs
--
-- Measured on 2026-08-08, not inferred. That is every user's profile, credit
-- balance, spending history, score history and job matches, readable by
-- anyone who opens devtools on the marketing site.
--
-- This is NOT only about V2_003. `agent_runs` is a V2_001 table whose RLS
-- block I had assumed was applied, and it is exposed too — so the question is
-- how much of the intended RLS has ever actually been in force.
--
-- IMPORTANT CONTEXT, so this is judged fairly: Bible §26.11 records that V1
-- deliberately used a permissive "app-server RLS" pattern
-- (`for all to anon, authenticated using (true)`) on later tables, because
-- V1's Streamlit app shares one cached anon client that cannot supply
-- auth.uid(). Access was gated in application code instead. So some of this
-- may be the known, deliberate V1 posture rather than a regression — but
-- `profiles`, `credits`, `credit_ledger` and `score_history` were documented
-- as having STRICT per-user policies, and they do not behave that way.
--
-- This file CHANGES NOTHING. It only reports, because the fix differs
-- depending on whether RLS is off or on-with-a-permissive-policy, and I
-- cannot see pg_catalog through the REST API.
-- ───────────────────────────────────────────────────────────────────────────


-- 1. Is RLS enabled, and how many policies does each table have?
select
    c.relname                                   as table_name,
    c.relrowsecurity                            as rls_enabled,
    c.relforcerowsecurity                       as rls_forced,
    (select count(*) from pg_policies p
      where p.schemaname = 'public' and p.tablename = c.relname) as policies
  from pg_class c
  join pg_namespace n on n.oid = c.relnamespace
 where n.nspname = 'public'
   and c.relkind = 'r'
 order by c.relrowsecurity, c.relname;

-- Read it like this:
--   rls_enabled = false            -> table is wide open to anon. Worst case.
--   rls_enabled = true, policies=0 -> anon and authenticated get NOTHING.
--                                     Correct for admin_notes/credit_requests.
--   rls_enabled = true, policies>0 -> depends entirely on the policy. See #2.


-- 2. What do the policies actually SAY? A policy granted to `anon` with a
--    `true` qualifier is functionally no protection at all.
select
    tablename,
    policyname,
    roles,
    cmd,
    qual        as using_expression,
    with_check
  from pg_policies
 where schemaname = 'public'
 order by tablename, policyname;

-- Look for: roles containing {anon} together with using_expression = 'true'.
-- That is the V1 "app-server RLS" pattern, and it means the database is not
-- enforcing anything — only application code is.


-- 3. The tables that matter most, called out explicitly.
select
    c.relname as table_name,
    c.relrowsecurity as rls_enabled,
    coalesce(
        (select string_agg(p.policyname || ' [' || array_to_string(p.roles, ',') ||
                           '] ' || coalesce(p.qual, '-'), ' | ')
           from pg_policies p
          where p.schemaname = 'public' and p.tablename = c.relname),
        '(no policies)'
    ) as policy_detail
  from pg_class c
  join pg_namespace n on n.oid = c.relnamespace
 where n.nspname = 'public'
   and c.relname in (
       'profiles', 'credits', 'credit_ledger', 'score_history', 'jobs_feed',
       'agent_runs', 'weekly_contracts', 'naukri_scores', 'thread_decisions',
       'admin_notes', 'credit_requests'
   )
 order by c.relname;
