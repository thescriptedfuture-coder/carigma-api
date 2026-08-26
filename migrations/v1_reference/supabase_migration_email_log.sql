-- ════════════════════════════════════════════════════════════════════════
-- Carigma — Email Log (Bug-fix brief 4.2 · admin email oversight)
-- One row per email the APP itself sends (daily brief, weekly review, and
-- later: welcome, payment receipts, custom sends): who / what / when / status.
-- This is the audit trail the Admin "Emails" tab reads.
--
-- NOT logged here: verification + password-reset emails — those are sent by
-- Supabase Auth's own system, not our app. See them in Supabase → Authentication
-- → Logs. (The Admin tab documents this split.)
--
-- digest_log stays as-is (the once-per-day dedup GUARD); email_log is the
-- record of actual send attempts, so a failed send is visible too.
-- Run ONCE:  Supabase → SQL Editor → + New query → paste CONTENTS → Run.
-- ════════════════════════════════════════════════════════════════════════

create table if not exists public.email_log (
    id          bigint generated always as identity primary key,
    user_id     uuid references auth.users (id) on delete set null,  -- nullable: some sends aren't user-scoped
    recipient   text not null,
    email_type  text not null,               -- daily | weekly | welcome | receipt | custom
    subject     text,
    status      text not null default 'sent',-- sent | failed
    error       text,                         -- short reason when status = failed
    created_at  timestamptz not null default now()
);

create index if not exists email_log_created_idx on public.email_log (created_at desc);

alter table public.email_log enable row level security;
drop policy if exists "email_log app access" on public.email_log;
create policy "email_log app access" on public.email_log
    for all to anon, authenticated using (true) with check (true);
