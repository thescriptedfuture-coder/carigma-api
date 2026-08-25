-- ════════════════════════════════════════════════════════════════════════
-- ResumeIQ — Job Application Tracker
-- One row per tracked application (Kanban: applied → interview → offer → rejected).
-- Run ONCE:  Supabase → SQL Editor → paste → Run.  Safe to re-run.
-- ════════════════════════════════════════════════════════════════════════

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

-- ── Row Level Security: each user can touch ONLY their own applications ────
alter table public.job_applications enable row level security;

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
