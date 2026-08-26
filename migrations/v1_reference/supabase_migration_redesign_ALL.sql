-- ═══════════════════════════════════════════════════════════════════════════
-- Carigma — ALL redesign migrations in ONE file (Phases 2–5b).
-- Every statement is idempotent (IF NOT EXISTS / DROP-then-CREATE policies),
-- so this is SAFE to run even if some parts were already applied.
--
-- HOW TO RUN (avoids the "snippet doesn't exist" dashboard glitch):
--   Supabase → SQL Editor → click "+ New query" (a FRESH tab, don't reuse an
--   old one) → paste this file's CONTENTS → Run.
-- Expected result: "Success. No rows returned".
-- ═══════════════════════════════════════════════════════════════════════════


-- ▼▼▼ 1/5 · supabase_migration_onboarding.sql (Phase 2 — the Decode) ▼▼▼

alter table public.profiles add column if not exists onboarded   boolean;
alter table public.profiles add column if not exists target_role text;
alter table public.profiles add column if not exists cadence     integer;

update public.profiles
   set onboarded = true
 where onboarded is null
   and coalesce(name, '') <> '';


-- ▼▼▼ 2/5 · supabase_migration_today.sql (Phase 3 — Today view) ▼▼▼

alter table public.profiles add column if not exists last_seen_jobs_at timestamptz;


-- ▼▼▼ 3/5 · supabase_migration_content_loop.sql (Phase 4 — content loop) ▼▼▼

create table if not exists public.content_plan (
    id            bigint generated always as identity primary key,
    user_id       uuid not null references auth.users (id) on delete cascade,
    week_start    date not null,
    slot_date     date not null,
    post_type     text,
    topic         text,
    hook          text,
    body          text,
    hashtags      text,
    visual_prompt text,
    status        text not null default 'draft',
    skip_reason   text,
    posted_at     timestamptz,
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now(),
    unique (user_id, slot_date)
);
create index if not exists content_plan_user_week_idx
    on public.content_plan (user_id, week_start desc);

create table if not exists public.content_feedback (
    id         bigint generated always as identity primary key,
    user_id    uuid not null references auth.users (id) on delete cascade,
    slot_id    bigint references public.content_plan (id) on delete set null,
    reason     text not null,
    created_at timestamptz not null default now()
);
create index if not exists content_feedback_user_idx
    on public.content_feedback (user_id, created_at desc);

create table if not exists public.milestones (
    id            bigint generated always as identity primary key,
    user_id       uuid not null references auth.users (id) on delete cascade,
    milestone_key text not null,
    achieved_at   timestamptz not null default now(),
    unique (user_id, milestone_key)
);

alter table public.profiles add column if not exists preferences jsonb default '{}'::jsonb;

alter table public.content_plan     enable row level security;
alter table public.content_feedback enable row level security;
alter table public.milestones       enable row level security;

drop policy if exists "content_plan app access" on public.content_plan;
create policy "content_plan app access" on public.content_plan
    for all to anon, authenticated using (true) with check (true);
drop policy if exists "content_feedback app access" on public.content_feedback;
create policy "content_feedback app access" on public.content_feedback
    for all to anon, authenticated using (true) with check (true);
drop policy if exists "milestones app access" on public.milestones;
create policy "milestones app access" on public.milestones
    for all to anon, authenticated using (true) with check (true);


-- ▼▼▼ 4/5 · supabase_migration_tracker_events.sql (Phase 5a — tracker events) ▼▼▼

alter table public.job_applications add column if not exists interview_on   date;
alter table public.job_applications add column if not exists closed_reason  text;

-- job_applications.id is a UUID in this project → application_id is uuid.
create table if not exists public.interview_preps (
    id             bigint generated always as identity primary key,
    user_id        uuid not null references auth.users (id) on delete cascade,
    application_id uuid not null references public.job_applications (id) on delete cascade,
    prep           jsonb not null,
    created_at     timestamptz not null default now(),
    unique (application_id)
);

create table if not exists public.application_events (
    id             bigint generated always as identity primary key,
    user_id        uuid not null references auth.users (id) on delete cascade,
    application_id uuid not null references public.job_applications (id) on delete cascade,
    event_type     text not null,
    note           text,
    created_at     timestamptz not null default now()
);
create index if not exists application_events_app_idx
    on public.application_events (application_id, created_at desc);

alter table public.interview_preps    enable row level security;
alter table public.application_events enable row level security;

drop policy if exists "interview_preps app access" on public.interview_preps;
create policy "interview_preps app access" on public.interview_preps
    for all to anon, authenticated using (true) with check (true);
drop policy if exists "application_events app access" on public.application_events;
create policy "application_events app access" on public.application_events
    for all to anon, authenticated using (true) with check (true);


-- ▼▼▼ 5/5 · supabase_migration_jobs_feed.sql (Phase 5b — jobs feed) ▼▼▼

create table if not exists public.jobs_feed (
    id             bigint generated always as identity primary key,
    user_id        uuid not null references auth.users (id) on delete cascade,
    dedup_key      text not null,
    title          text,
    company        text,
    location       text,
    job_type       text,
    salary         text,
    match_score    integer default 0,
    why_match      text,
    details        jsonb default '{}'::jsonb,
    status         text not null default 'active',
    dismiss_reason text,
    apply_kit      jsonb,
    kit_created_at timestamptz,
    first_seen     timestamptz not null default now(),
    last_seen      timestamptz not null default now(),
    unique (user_id, dedup_key)
);
create index if not exists jobs_feed_user_idx
    on public.jobs_feed (user_id, last_seen desc);

alter table public.jobs_feed enable row level security;
drop policy if exists "jobs_feed app access" on public.jobs_feed;
create policy "jobs_feed app access" on public.jobs_feed
    for all to anon, authenticated using (true) with check (true);

-- ▼▼▼ 6/6 · supabase_migration_digests.sql (Phase 6 — email digests) ▼▼▼

create table if not exists public.digest_log (
    id          bigint generated always as identity primary key,
    user_id     uuid not null references auth.users (id) on delete cascade,
    digest_type text not null,
    sent_on     date not null,
    created_at  timestamptz not null default now(),
    unique (user_id, digest_type, sent_on)
);

alter table public.digest_log enable row level security;
drop policy if exists "digest_log app access" on public.digest_log;
create policy "digest_log app access" on public.digest_log
    for all to anon, authenticated using (true) with check (true);

-- ═══════════════════════════════════════════════════════════════════════════
-- Done. All redesign migrations applied (Phases 2–6).
-- ═══════════════════════════════════════════════════════════════════════════
