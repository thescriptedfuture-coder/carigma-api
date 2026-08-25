-- ════════════════════════════════════════════════════════════════════════
-- ResumeIQ — June 2026 enhancements migration
-- Additive & idempotent (safe to re-run). Paste into Supabase → SQL Editor → Run.
--
-- Covers DB needs surfaced by the latest round of work:
--   1. Persistent headshots  (fixes the "headshot wiped on Render redeploy" bug)
--   2. LinkedIn URL auto-sync metadata  (supports URL-based profile sync)
--   3. Subscriptions / billing scaffold  (Phase 4 monetisation)
--
-- NOTE: The session-persistence and "5 MB upload" fixes need NO database change.
-- All currently-shipped features (campaign brief, job tracker, post history,
-- score history, usage/plan) are already covered by supabase_schema.sql +
-- the existing per-feature migration files — make sure those have been run.
-- ════════════════════════════════════════════════════════════════════════

-- ── 1. Persistent headshot --------------------------------------------------
-- Today the headshot is written to local disk on Render and lost on every
-- redeploy. Store it in a Supabase Storage bucket and keep the URL here.
alter table public.profiles add column if not exists headshot_url text;

-- Create a public-read bucket for headshots (no-op if it already exists).
insert into storage.buckets (id, name, public)
values ('headshots', 'headshots', true)
on conflict (id) do nothing;

-- Storage RLS: a user may only write/replace/delete files under a folder named
-- after their own user id (e.g.  headshots/<uid>/photo.jpg). Reads are public.
drop policy if exists "headshots public read"   on storage.objects;
drop policy if exists "headshots owner write"   on storage.objects;
drop policy if exists "headshots owner update"  on storage.objects;
drop policy if exists "headshots owner delete"  on storage.objects;

create policy "headshots public read" on storage.objects
    for select using (bucket_id = 'headshots');
create policy "headshots owner write" on storage.objects
    for insert with check (
        bucket_id = 'headshots'
        and (storage.foldername(name))[1] = auth.uid()::text
    );
create policy "headshots owner update" on storage.objects
    for update using (
        bucket_id = 'headshots'
        and (storage.foldername(name))[1] = auth.uid()::text
    );
create policy "headshots owner delete" on storage.objects
    for delete using (
        bucket_id = 'headshots'
        and (storage.foldername(name))[1] = auth.uid()::text
    );

-- ── 2. LinkedIn URL auto-sync metadata -------------------------------------
-- linkedin_url already exists. These record WHEN a URL sync last ran and cache
-- the raw provider payload so repeat lookups don't re-bill the enrichment API.
alter table public.profiles add column if not exists linkedin_synced_at timestamptz;
alter table public.profiles add column if not exists linkedin_raw       jsonb;

-- ── 3. Subscriptions / billing scaffold ------------------------------------
-- `plan` on profiles is the fast read for gating. This table is the source of
-- truth once Razorpay (India) / Stripe (intl) is wired in: one row per user's
-- active (or past) subscription.
create table if not exists public.subscriptions (
    id                   uuid primary key default gen_random_uuid(),
    user_id              uuid not null references auth.users (id) on delete cascade,
    plan                 text not null default 'free',     -- free | pro | premium
    status               text not null default 'inactive', -- active | inactive | past_due | cancelled
    provider             text,                              -- razorpay | stripe
    provider_customer_id text,
    provider_sub_id      text,
    current_period_end   timestamptz,
    created_at           timestamptz not null default now(),
    updated_at           timestamptz not null default now()
);
create index if not exists subscriptions_user_idx
    on public.subscriptions (user_id, status);

alter table public.subscriptions enable row level security;

-- Users may READ their own subscription. Writes go through the server / a
-- payment webhook using the service key, so no user insert/update policy here.
drop policy if exists "own subscription select" on public.subscriptions;
create policy "own subscription select" on public.subscriptions
    for select using (auth.uid() = user_id);
