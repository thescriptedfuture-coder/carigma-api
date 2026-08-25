-- ════════════════════════════════════════════════════════════════════════
-- Carigma — Today view (Experience Master Spec Phase 3)
-- Tracks when the user last opened the Jobs page, so "N new matches" on
-- Today is derived from events we own (Scout run newer than last visit).
-- Run ONCE:  Supabase → SQL Editor → paste CONTENTS → Run.  Safe to re-run.
-- Fails open pre-migration: without the column, matches simply count as new.
-- ════════════════════════════════════════════════════════════════════════

alter table public.profiles add column if not exists last_seen_jobs_at timestamptz;
