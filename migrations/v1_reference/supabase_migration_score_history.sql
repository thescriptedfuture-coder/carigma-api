-- ════════════════════════════════════════════════════════════════════════
-- ResumeIQ — Profile Score History
-- Stores each profile-score result so the Glow-Up Guru can chart progress
-- over time. Run ONCE:  Supabase → SQL Editor → paste → Run. Safe to re-run.
-- ════════════════════════════════════════════════════════════════════════

create table if not exists public.score_history (
    id         uuid primary key default gen_random_uuid(),
    user_id    uuid not null references auth.users (id) on delete cascade,
    score      int not null,
    created_at timestamptz not null default now()
);

create index if not exists score_history_user_created_idx
    on public.score_history (user_id, created_at);

-- ── Row Level Security: each user can touch ONLY their own scores ─────────
alter table public.score_history enable row level security;

drop policy if exists "own score select" on public.score_history;
drop policy if exists "own score insert" on public.score_history;
create policy "own score select" on public.score_history
    for select using (auth.uid() = user_id);
create policy "own score insert" on public.score_history
    for insert with check (auth.uid() = user_id);
