-- ════════════════════════════════════════════════════════════════════════
-- Carigma — Jobs feed (Experience Spec 3.4 · Phase 5b, per PHASE5_HANDOFF.md)
-- Matches persist across rescans and logouts: a rescan UPDATEs last_seen on
-- jobs it sees again (keeping first_seen — powers the "New today" chip) and
-- INSERTs genuinely new ones. Apply kits are stored per job (idempotent —
-- reopening is free). Not-for-me dismissals keep the row (status=dismissed)
-- so the same job never resurfaces.
-- Run ONCE:  Supabase → SQL Editor → paste CONTENTS → Run.  Safe to re-run.
-- ════════════════════════════════════════════════════════════════════════

create table if not exists public.jobs_feed (
    id             bigint generated always as identity primary key,
    user_id        uuid not null references auth.users (id) on delete cascade,
    dedup_key      text not null,             -- sha1(company+title), stable across scans
    title          text,
    company        text,
    location       text,
    job_type       text,
    salary         text,
    match_score    integer default 0,
    why_match      text,
    details        jsonb default '{}'::jsonb, -- requirements/links/tweaks/outreach…
    status         text not null default 'active',   -- active | dismissed | saved
    dismiss_reason text,
    apply_kit      jsonb,                     -- {cv, template, outreach, gaps} once generated
    kit_created_at timestamptz,
    first_seen     timestamptz not null default now(),
    last_seen      timestamptz not null default now(),
    unique (user_id, dedup_key)
);
create index if not exists jobs_feed_user_idx
    on public.jobs_feed (user_id, last_seen desc);

-- RLS: server-side app pattern (anon key is server-only; queries user-scoped).
alter table public.jobs_feed enable row level security;
drop policy if exists "jobs_feed app access" on public.jobs_feed;
create policy "jobs_feed app access" on public.jobs_feed
    for all to anon, authenticated using (true) with check (true);
