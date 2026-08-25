-- ════════════════════════════════════════════════════════════════════════
-- Carigma — email digests (Experience Spec 3.12 · Phase 6)
-- digest_log = the duplicate-send guard: one row per user per digest type per
-- day (unique constraint). The cron script INSERTs before sending; a conflict
-- means "already sent today" and the user is skipped.
-- Email on/off preferences live in profiles.preferences (no new column).
-- Run ONCE:  Supabase → SQL Editor → + New query → paste CONTENTS → Run.
-- ════════════════════════════════════════════════════════════════════════

create table if not exists public.digest_log (
    id          bigint generated always as identity primary key,
    user_id     uuid not null references auth.users (id) on delete cascade,
    digest_type text not null,               -- daily | weekly
    sent_on     date not null,
    created_at  timestamptz not null default now(),
    unique (user_id, digest_type, sent_on)
);

alter table public.digest_log enable row level security;
drop policy if exists "digest_log app access" on public.digest_log;
create policy "digest_log app access" on public.digest_log
    for all to anon, authenticated using (true) with check (true);
