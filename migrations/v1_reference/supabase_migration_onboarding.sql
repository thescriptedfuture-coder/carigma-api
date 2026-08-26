-- ════════════════════════════════════════════════════════════════════════
-- Carigma — onboarding "Decode" (Experience Master Spec Phase 2)
-- Adds the onboarding flag + Step-4 target fields to profiles, and marks all
-- EXISTING named profiles as onboarded so current users are never forced
-- through the new flow. (City reuses the existing `location` column.)
-- Run ONCE:  Supabase → SQL Editor → paste CONTENTS → Run.  Safe to re-run.
-- The app fails open pre-migration: without these columns the legacy
-- "has a name = onboarded" heuristic applies.
-- ════════════════════════════════════════════════════════════════════════

alter table public.profiles add column if not exists onboarded   boolean;
alter table public.profiles add column if not exists target_role text;
alter table public.profiles add column if not exists cadence     integer;

update public.profiles
   set onboarded = true
 where onboarded is null
   and coalesce(name, '') <> '';
