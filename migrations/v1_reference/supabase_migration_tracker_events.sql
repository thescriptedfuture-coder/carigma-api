-- ════════════════════════════════════════════════════════════════════════
-- Carigma — Tracker stage events (Experience Spec Phase 5 · 3.5, 3.7, 9.4)
--   job_applications: + interview_on (user-entered date → powers Today),
--                     + closed_reason (Rejected/Withdrew/Ghosted/Accepted elsewhere)
--   interview_preps:  one persisted prep kit per application (reopen = free)
--   application_events: the Opportunity Timeline — ONLY events we own or the
--                     user logs (guardrail 8.5: no fabricated "recruiter viewed")
-- Run ONCE:  Supabase → SQL Editor → paste CONTENTS → Run.  Safe to re-run.
-- ════════════════════════════════════════════════════════════════════════

alter table public.job_applications add column if not exists interview_on   date;
alter table public.job_applications add column if not exists closed_reason  text;

-- NOTE: job_applications.id is a UUID (see supabase_migration_job_tracker.sql),
-- so application_id must be uuid too.
create table if not exists public.interview_preps (
    id             bigint generated always as identity primary key,
    user_id        uuid not null references auth.users (id) on delete cascade,
    application_id uuid not null references public.job_applications (id) on delete cascade,
    prep           jsonb not null,
    created_at     timestamptz not null default now(),
    unique (application_id)                 -- one kit per application (idempotent)
);

create table if not exists public.application_events (
    id             bigint generated always as identity primary key,
    user_id        uuid not null references auth.users (id) on delete cascade,
    application_id uuid not null references public.job_applications (id) on delete cascade,
    event_type     text not null,           -- stage_saved/applied/interview/offer/closed · note · prep_built
    note           text,
    created_at     timestamptz not null default now()
);
create index if not exists application_events_app_idx
    on public.application_events (application_id, created_at desc);

-- RLS: server-side app pattern (anon key is server-only; queries user-scoped).
alter table public.interview_preps    enable row level security;
alter table public.application_events enable row level security;

drop policy if exists "interview_preps app access" on public.interview_preps;
create policy "interview_preps app access" on public.interview_preps
    for all to anon, authenticated using (true) with check (true);
drop policy if exists "application_events app access" on public.application_events;
create policy "application_events app access" on public.application_events
    for all to anon, authenticated using (true) with check (true);
