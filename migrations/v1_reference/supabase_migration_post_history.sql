-- ════════════════════════════════════════════════════════════════════════
-- ResumeIQ — Post History
-- Stores every Captain Hook batch so past runs build a library instead of
-- being overwritten. Run ONCE:  Supabase → SQL Editor → paste → Run.
-- Safe to re-run.
-- ════════════════════════════════════════════════════════════════════════

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

-- ── Row Level Security: each user can touch ONLY their own batches ─────────
alter table public.post_history enable row level security;

drop policy if exists "own post_history select" on public.post_history;
drop policy if exists "own post_history insert" on public.post_history;
drop policy if exists "own post_history delete" on public.post_history;
create policy "own post_history select" on public.post_history
    for select using (auth.uid() = user_id);
create policy "own post_history insert" on public.post_history
    for insert with check (auth.uid() = user_id);
create policy "own post_history delete" on public.post_history
    for delete using (auth.uid() = user_id);
