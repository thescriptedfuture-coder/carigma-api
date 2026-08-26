-- ════════════════════════════════════════════════════════════════════════
-- Carigma — in-app feedback
-- Stores beta feedback submitted from the sidebar 💬 widget.
-- Run ONCE:  Supabase → SQL Editor → paste → Run.  Safe to re-run.
-- ════════════════════════════════════════════════════════════════════════

create table if not exists public.feedback (
    id         bigint generated always as identity primary key,
    user_id    uuid references auth.users (id) on delete set null,
    email      text,
    rating     integer,                       -- 1..5 (optional)
    message    text not null,
    page       text,                          -- which page they were on
    created_at timestamptz not null default now()
);

create index if not exists feedback_created_idx
    on public.feedback (created_at desc);

-- RLS: a signed-in user may INSERT their own feedback; nobody reads via the
-- anon key (reads happen later from the admin/service-role client only).
alter table public.feedback enable row level security;

drop policy if exists "insert own feedback" on public.feedback;
create policy "insert own feedback" on public.feedback
    for insert with check (auth.uid() = user_id);
