-- ════════════════════════════════════════════════════════════════════════
-- Carigma — content loop + milestones + AI memory (Experience Spec Phase 4,
-- Parts 3.3, 9.1, 9.2)
--   content_plan:     one row per planned post slot (draft → posted/skipped)
--   content_feedback: skip reasons — fed into the NEXT plan generation prompt
--   milestones:       fire-once achievement flags (idempotent by unique key)
--   profiles.preferences: the AI-memory JSONB every agent prompt can read
-- Run ONCE:  Supabase → SQL Editor → paste CONTENTS → Run.  Safe to re-run.
-- ════════════════════════════════════════════════════════════════════════

create table if not exists public.content_plan (
    id            bigint generated always as identity primary key,
    user_id       uuid not null references auth.users (id) on delete cascade,
    week_start    date not null,                -- Monday of the plan week
    slot_date     date not null,
    post_type     text,                         -- text | photo | carousel | poll
    topic         text,
    hook          text,
    body          text,
    hashtags      text,
    visual_prompt text,
    status        text not null default 'draft',   -- draft | posted | skipped
    skip_reason   text,
    posted_at     timestamptz,
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now(),
    unique (user_id, slot_date)                 -- one slot per day keeps regen idempotent
);
create index if not exists content_plan_user_week_idx
    on public.content_plan (user_id, week_start desc);

create table if not exists public.content_feedback (
    id         bigint generated always as identity primary key,
    user_id    uuid not null references auth.users (id) on delete cascade,
    slot_id    bigint references public.content_plan (id) on delete set null,
    reason     text not null,                   -- Not my voice | Wrong topic | No time this week
    created_at timestamptz not null default now()
);
create index if not exists content_feedback_user_idx
    on public.content_feedback (user_id, created_at desc);

create table if not exists public.milestones (
    id            bigint generated always as identity primary key,
    user_id       uuid not null references auth.users (id) on delete cascade,
    milestone_key text not null,
    achieved_at   timestamptz not null default now(),
    unique (user_id, milestone_key)             -- fire exactly once, forever
);

alter table public.profiles add column if not exists preferences jsonb default '{}'::jsonb;

-- RLS: server-side app pattern (same as payments/feedback fix) — the anon key
-- is server-only and every query is user_id-scoped in the app.
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
