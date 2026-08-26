-- ════════════════════════════════════════════════════════════════════════
-- ResumeIQ — Credits (in-app currency)
-- Adds a per-user credit balance + an audit ledger of every change.
-- Run ONCE:  Supabase → SQL Editor → paste → Run.  Safe to re-run.
--
-- New users are granted their one-time welcome credits by the app on first
-- sign-in (SIGNUP_CREDITS in resumeiq/config.py). No DB trigger needed.
-- ════════════════════════════════════════════════════════════════════════

-- One balance row per user
create table if not exists public.credits (
    user_id    uuid primary key references auth.users (id) on delete cascade,
    balance    integer not null default 0,
    updated_at timestamptz not null default now()
);

-- Audit trail: one row per grant / spend (delta < 0 = spent, > 0 = granted)
create table if not exists public.credit_ledger (
    id            bigint generated always as identity primary key,
    user_id       uuid not null references auth.users (id) on delete cascade,
    delta         integer not null,
    reason        text,
    balance_after integer,
    created_at    timestamptz not null default now()
);

create index if not exists credit_ledger_user_idx
    on public.credit_ledger (user_id, created_at desc);

-- ── Row Level Security: each user can touch ONLY their own rows ────────────
alter table public.credits        enable row level security;
alter table public.credit_ledger  enable row level security;

drop policy if exists "own credits select" on public.credits;
drop policy if exists "own credits insert" on public.credits;
drop policy if exists "own credits update" on public.credits;
create policy "own credits select" on public.credits
    for select using (auth.uid() = user_id);
create policy "own credits insert" on public.credits
    for insert with check (auth.uid() = user_id);
create policy "own credits update" on public.credits
    for update using (auth.uid() = user_id) with check (auth.uid() = user_id);

drop policy if exists "own ledger select" on public.credit_ledger;
drop policy if exists "own ledger insert" on public.credit_ledger;
create policy "own ledger select" on public.credit_ledger
    for select using (auth.uid() = user_id);
create policy "own ledger insert" on public.credit_ledger
    for insert with check (auth.uid() = user_id);
