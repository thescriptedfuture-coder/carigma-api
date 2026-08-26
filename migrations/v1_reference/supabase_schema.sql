-- ════════════════════════════════════════════════════════════════════════
-- ResumeIQ — Supabase schema (flat-column tables)
-- Run this ONCE in a fresh Supabase project:  SQL Editor → New query → Run.
-- For an EXISTING project that predates the Campaign Brief, run
-- supabase_migration_campaign_brief.sql instead (additive, safe to re-run).
-- ════════════════════════════════════════════════════════════════════════

-- One row per user holding their profile fields
create table if not exists public.profiles (
    user_id           uuid primary key references auth.users (id) on delete cascade,
    name              text,
    current_role      text,
    industry          text,
    skills            text,
    target_roles      text,
    location          text,
    experience        text,
    education         text,
    certifications    text,
    linkedin_headline text,
    about_section     text,
    niche             text,
    tone              text,
    posting_frequency text,
    cv_template       text,
    last_synced       text,
    -- Campaign Brief
    primary_goal      text,
    personal_brand    text,
    target_sectors    text,
    key_achievement   text,
    content_avoid     text,
    linkedin_url      text,
    plan              text default 'free',
    updated_at        timestamptz not null default now()
);

-- One row per user per month tracking agent runs used (Free-plan limits)
create table if not exists public.usage (
    user_id    uuid not null references auth.users (id) on delete cascade,
    period     text not null,
    runs       int not null default 0,
    updated_at timestamptz not null default now(),
    primary key (user_id, period)
);

-- One row per user holding their latest agent-run results
create table if not exists public.results (
    user_id      uuid primary key references auth.users (id) on delete cascade,
    posts        jsonb not null default '[]'::jsonb,
    jobs         jsonb not null default '[]'::jsonb,
    profile_data jsonb not null default '{}'::jsonb,
    last_run     text,
    updated_at   timestamptz not null default now()
);

-- One row per archived Captain Hook batch (post history / content library)
create table if not exists public.post_history (
    id            uuid primary key default gen_random_uuid(),
    user_id       uuid not null references auth.users (id) on delete cascade,
    posts         jsonb not null default '[]'::jsonb,
    campaign_goal text,
    post_count    int,
    created_at    timestamptz not null default now()
);
create index if not exists post_history_user_created_idx
    on public.post_history (user_id, created_at desc);

-- One row per tracked job application (Kanban board)
create table if not exists public.job_applications (
    id         uuid primary key default gen_random_uuid(),
    user_id    uuid not null references auth.users (id) on delete cascade,
    company    text,
    role       text,
    status     text not null default 'applied',   -- applied | interview | offer | rejected
    link       text,
    salary     text,
    notes      text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);
create index if not exists job_applications_user_idx
    on public.job_applications (user_id, status, created_at desc);

-- One row per profile-score audit (trend over time)
create table if not exists public.score_history (
    id         uuid primary key default gen_random_uuid(),
    user_id    uuid not null references auth.users (id) on delete cascade,
    score      int not null,
    created_at timestamptz not null default now()
);
create index if not exists score_history_user_created_idx
    on public.score_history (user_id, created_at);

-- ── Row Level Security: each user can touch ONLY their own row ─────────────
alter table public.profiles         enable row level security;
alter table public.results          enable row level security;
alter table public.post_history     enable row level security;
alter table public.job_applications enable row level security;
alter table public.score_history    enable row level security;
alter table public.usage            enable row level security;

-- profiles policies
drop policy if exists "own profile select" on public.profiles;
drop policy if exists "own profile upsert" on public.profiles;
drop policy if exists "own profile update" on public.profiles;
create policy "own profile select" on public.profiles
    for select using (auth.uid() = user_id);
create policy "own profile upsert" on public.profiles
    for insert with check (auth.uid() = user_id);
create policy "own profile update" on public.profiles
    for update using (auth.uid() = user_id) with check (auth.uid() = user_id);

-- results policies
drop policy if exists "own results select" on public.results;
drop policy if exists "own results upsert" on public.results;
drop policy if exists "own results update" on public.results;
create policy "own results select" on public.results
    for select using (auth.uid() = user_id);
create policy "own results upsert" on public.results
    for insert with check (auth.uid() = user_id);
create policy "own results update" on public.results
    for update using (auth.uid() = user_id) with check (auth.uid() = user_id);

-- post_history policies
drop policy if exists "own post_history select" on public.post_history;
drop policy if exists "own post_history insert" on public.post_history;
drop policy if exists "own post_history delete" on public.post_history;
create policy "own post_history select" on public.post_history
    for select using (auth.uid() = user_id);
create policy "own post_history insert" on public.post_history
    for insert with check (auth.uid() = user_id);
create policy "own post_history delete" on public.post_history
    for delete using (auth.uid() = user_id);

-- job_applications policies
drop policy if exists "own jobs select" on public.job_applications;
drop policy if exists "own jobs insert" on public.job_applications;
drop policy if exists "own jobs update" on public.job_applications;
drop policy if exists "own jobs delete" on public.job_applications;
create policy "own jobs select" on public.job_applications
    for select using (auth.uid() = user_id);
create policy "own jobs insert" on public.job_applications
    for insert with check (auth.uid() = user_id);
create policy "own jobs update" on public.job_applications
    for update using (auth.uid() = user_id) with check (auth.uid() = user_id);
create policy "own jobs delete" on public.job_applications
    for delete using (auth.uid() = user_id);

-- score_history policies
drop policy if exists "own score select" on public.score_history;
drop policy if exists "own score insert" on public.score_history;
create policy "own score select" on public.score_history
    for select using (auth.uid() = user_id);
create policy "own score insert" on public.score_history
    for insert with check (auth.uid() = user_id);

-- usage policies
drop policy if exists "own usage select" on public.usage;
drop policy if exists "own usage upsert" on public.usage;
drop policy if exists "own usage update" on public.usage;
create policy "own usage select" on public.usage
    for select using (auth.uid() = user_id);
create policy "own usage upsert" on public.usage
    for insert with check (auth.uid() = user_id);
create policy "own usage update" on public.usage
    for update using (auth.uid() = user_id) with check (auth.uid() = user_id);

-- ════════════════════════════════════════════════════════════════════════
-- June 2026 enhancements (folded in so this file is the complete source of
-- truth). All additive & idempotent — safe to re-run on an existing project.
-- ════════════════════════════════════════════════════════════════════════

-- ── Persistent headshot: column + public-read Storage bucket ──────────────
-- The app stores a CV headshot at  headshots/<uid>/headshot.jpg  so it
-- survives a Render redeploy (local disk is wiped on every deploy).
alter table public.profiles add column if not exists headshot_url       text;
alter table public.profiles add column if not exists linkedin_synced_at timestamptz;
alter table public.profiles add column if not exists linkedin_raw       jsonb;

insert into storage.buckets (id, name, public)
values ('headshots', 'headshots', true)
on conflict (id) do nothing;

-- Storage RLS: reads are public; a user may only write/replace/delete files
-- under a folder named after their own user id.
drop policy if exists "headshots public read"  on storage.objects;
drop policy if exists "headshots owner write"  on storage.objects;
drop policy if exists "headshots owner update" on storage.objects;
drop policy if exists "headshots owner delete" on storage.objects;
create policy "headshots public read" on storage.objects
    for select using (bucket_id = 'headshots');
create policy "headshots owner write" on storage.objects
    for insert with check (
        bucket_id = 'headshots' and (storage.foldername(name))[1] = auth.uid()::text);
create policy "headshots owner update" on storage.objects
    for update using (
        bucket_id = 'headshots' and (storage.foldername(name))[1] = auth.uid()::text);
create policy "headshots owner delete" on storage.objects
    for delete using (
        bucket_id = 'headshots' and (storage.foldername(name))[1] = auth.uid()::text);

-- ── Subscriptions / billing source-of-truth (plan column stays the fast read)
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
drop policy if exists "own subscription select" on public.subscriptions;
create policy "own subscription select" on public.subscriptions
    for select using (auth.uid() = user_id);
