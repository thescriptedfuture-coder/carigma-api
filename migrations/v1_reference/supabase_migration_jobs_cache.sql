-- ════════════════════════════════════════════════════════════════════════
-- Carigma — shared jobs cache (Real Jobs Engine, JSearch+Adzuna)
-- One row per normalized (role × city × remote × freshness) query. Every user
-- searching the same thing SHARES this row instead of each burning an API call
-- — what keeps the free JSearch tier survivable. The per-user match %/ranking is
-- still computed per user; only the RAW jobs are shared here.
--
-- payload = the JSON array of already-normalized jobs. fetched_at drives the TTL
-- (JOBS_CACHE_TTL_HOURS) and the daily-cap counter (rows fetched today).
-- Run ONCE:  Supabase → SQL Editor → + New query → paste CONTENTS → Run.
-- ════════════════════════════════════════════════════════════════════════

create table if not exists public.jobs_cache (
    id          bigint generated always as identity primary key,
    query_key   text not null unique,        -- '{role}|{city}|{remote}|{date_posted}' (lowercased)
    source      text not null,               -- jsearch | adzuna | mixed
    payload     jsonb not null default '[]'::jsonb,
    fetched_at  timestamptz not null default now()
);

create index if not exists jobs_cache_fetched_idx on public.jobs_cache (fetched_at desc);

alter table public.jobs_cache enable row level security;
drop policy if exists "jobs_cache app access" on public.jobs_cache;
create policy "jobs_cache app access" on public.jobs_cache
    for all to anon, authenticated using (true) with check (true);
