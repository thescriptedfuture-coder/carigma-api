-- ════════════════════════════════════════════════════════════════════════
-- ResumeIQ — Campaign Brief persistence migration
-- Adds the columns that store the user's Campaign Brief + LinkedIn URL so the
-- data survives logout / login. Run ONCE:  Supabase → SQL Editor → paste → Run.
-- Safe to re-run (uses IF NOT EXISTS).
-- ════════════════════════════════════════════════════════════════════════

alter table public.profiles add column if not exists primary_goal    text;
alter table public.profiles add column if not exists personal_brand  text;
alter table public.profiles add column if not exists target_sectors  text;
alter table public.profiles add column if not exists key_achievement text;
alter table public.profiles add column if not exists content_avoid   text;
alter table public.profiles add column if not exists linkedin_url    text;
