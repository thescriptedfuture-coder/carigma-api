-- ════════════════════════════════════════════════════════════════════════
-- Carigma — feedback RLS fix
-- WHY: the app is server-side and talks to Supabase with the shared anon-key
-- client, so auth.uid() is not reliably the submitting user — the original
-- "insert own feedback" (auth.uid() = user_id) policy therefore rejected real
-- submissions with:  42501 "new row violates row-level security policy".
-- FIX: allow inserts from the app server. Feedback stays WRITE-ONLY for the
-- anon key: there is deliberately NO select/update/delete policy, so nobody
-- can read or tamper with it except the service-role (admin dashboard).
-- Run ONCE:  Supabase → SQL Editor → paste CONTENTS → Run.  Safe to re-run.
-- ════════════════════════════════════════════════════════════════════════

drop policy if exists "insert own feedback" on public.feedback;
drop policy if exists "insert feedback (app)" on public.feedback;

create policy "insert feedback (app)" on public.feedback
    for insert to anon, authenticated
    with check (true);
