-- ════════════════════════════════════════════════════════════════════════
-- ResumeIQ — Usage limits + Subscription
-- Adds a `plan` column to profiles and a monthly `usage` counter table.
-- Run ONCE:  Supabase → SQL Editor → paste → Run.  Safe to re-run.
-- ════════════════════════════════════════════════════════════════════════

-- Subscription plan on the profile (free | pro | premium)
alter table public.profiles add column if not exists plan text default 'free';

-- One row per user per month tracking agent runs used
create table if not exists public.usage (
    user_id    uuid not null references auth.users (id) on delete cascade,
    period     text not null,                 -- "YYYY-MM"
    runs       int not null default 0,
    updated_at timestamptz not null default now(),
    primary key (user_id, period)
);

-- ── Row Level Security: each user can touch ONLY their own usage rows ──────
alter table public.usage enable row level security;

drop policy if exists "own usage select" on public.usage;
drop policy if exists "own usage upsert" on public.usage;
drop policy if exists "own usage update" on public.usage;
create policy "own usage select" on public.usage
    for select using (auth.uid() = user_id);
create policy "own usage upsert" on public.usage
    for insert with check (auth.uid() = user_id);
create policy "own usage update" on public.usage
    for update using (auth.uid() = user_id) with check (auth.uid() = user_id);
