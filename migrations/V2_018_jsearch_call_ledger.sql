-- ───────────────────────────────────────────────────────────────────────────
-- V2_018 — the JSearch daily cap, counted somewhere a user request can reach
--
-- ## Why this exists
--
-- `JSEARCH_DAILY_CAP` protects a JSearch key that V1 and V2 share, and live V1
-- users have first claim on it. V2 counted against `jobs_cache` — through the
-- USER's client. V2_007 made `jobs_cache` service-role only (RLS on, no
-- policies), so for a signed-in user:
--
--   select … from jobs_cache   → 0 rows, no error
--   upsert … into jobs_cache   → 42501, swallowed by a "never raise" writer
--
-- The count therefore read 0 through every real scan, and the cap refused
-- nothing. Measured on 2026-10-17: three scans that day, 12 rows in the table,
-- the newest from 2026-08-11. V1 wrote this exact lesson into resumeiq/db.py
-- in August: "An anonymous count under RLS succeeds and returns 0 — no error
-- to catch."
--
-- ## Why not open `jobs_cache` to `authenticated`
--
-- Because it holds PAYLOADS that other users are served. A table any signed-in
-- user can write is a table any signed-in user can fill with a fake posting
-- and a phishing apply link, served to everyone searching that role and city
-- for 48 hours. V2_007 closed it for that reason and it stays closed.
--
-- ## What this adds instead
--
--   jobs_provider_calls   one row per provider call V2 makes, written as the
--                         caller, owner-scoped, and carrying NO payload — so
--                         there is nothing in it to poison.
--
--   jsearch_calls_today() SECURITY DEFINER, granted to `authenticated`. Adds
--                         V1's fetches today (jobs_cache rows, counted the
--                         way V1 counts them, plus `mixed`, which also called
--                         JSearch) to V2's calls today. Returns one integer
--                         and nothing else: no query, no user, no payload.
--
-- ## What it does not protect against
--
-- A user posting rows into their own `jobs_provider_calls` with a hand-made
-- request could push today's count to the cap. That FAILS CLOSED — V2 scans
-- stop until midnight UTC; V1, which counts only `jobs_cache`, is untouched.
-- The direction is the safe one, so it is accepted at this scale.
--
-- Additive: a new table and a new function. Nothing of V1's is altered.
-- ───────────────────────────────────────────────────────────────────────────

create table if not exists public.jobs_provider_calls (
    id         bigint generated always as identity primary key,
    user_id    uuid not null default auth.uid() references auth.users (id) on delete cascade,
    source     text not null check (source in ('jsearch', 'adzuna')),
    --: Readable, so a person looking at the table can tell what was searched.
    query_key  text not null,
    called_at  timestamptz not null default now()
);

create index if not exists jobs_provider_calls_source_called_idx
    on public.jobs_provider_calls (source, called_at desc);

alter table public.jobs_provider_calls enable row level security;

drop policy if exists "jobs_provider_calls own insert" on public.jobs_provider_calls;
create policy "jobs_provider_calls own insert" on public.jobs_provider_calls
    for insert to authenticated with check (auth.uid() = user_id);

-- SELECT too, because supabase-py asks for the inserted row back and Postgres
-- refuses an INSERT … RETURNING whose row fails the SELECT policy.
drop policy if exists "jobs_provider_calls own read" on public.jobs_provider_calls;
create policy "jobs_provider_calls own read" on public.jobs_provider_calls
    for select to authenticated using (auth.uid() = user_id);


create or replace function public.jsearch_calls_today()
returns integer
language sql
security definer
set search_path = public, pg_temp
stable
as $$
    select (
        (select count(*) from public.jobs_cache
          where source in ('jsearch', 'mixed')
            and fetched_at >= date_trunc('day', now() at time zone 'utc') at time zone 'utc')
      + (select count(*) from public.jobs_provider_calls
          where source = 'jsearch'
            and called_at >= date_trunc('day', now() at time zone 'utc') at time zone 'utc')
    )::integer;
$$;

-- Revoked from `public` and `anon` explicitly rather than left to the default,
-- because the default has changed before. A visitor with no account has no
-- scan to make and no reason to know the count.
revoke all on function public.jsearch_calls_today() from public;
revoke all on function public.jsearch_calls_today() from anon;
grant execute on function public.jsearch_calls_today() to authenticated;


-- ───────────────────────────────────────────────────────────────────────────
-- Verification
--
-- Expect: jobs_provider_calls rls=true policies=2, and a count (0 or more).
-- ───────────────────────────────────────────────────────────────────────────
select
    c.relname as table_name,
    c.relrowsecurity as rls,
    (select count(*) from pg_policies p
      where p.schemaname = 'public' and p.tablename = c.relname) as policy_count,
    public.jsearch_calls_today() as jsearch_calls_today
from pg_class c
join pg_namespace n on n.oid = c.relnamespace
where n.nspname = 'public' and c.relname = 'jobs_provider_calls';
