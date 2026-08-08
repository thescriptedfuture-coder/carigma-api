-- ═══════════════════════════════════════════════════════════════════════════
-- CARIGMA V2 — Migration 001: additive foundations
--
-- ⚠️  STRICTLY ADDITIVE. V1 (Streamlit) and V2 share ONE Supabase project and
--     V1 STAYS LIVE until cutover. Therefore this file contains:
--       • CREATE TABLE IF NOT EXISTS          — new tables only
--       • ALTER TABLE ... ADD COLUMN IF NOT EXISTS, NULLABLE with safe defaults
--     and contains NO:
--       • DROP / RENAME / ALTER COLUMN TYPE
--       • NOT NULL added to an existing column
--       • constraint tightening on anything V1 reads
--       • data backfill that changes what V1 would read
--
--     Every statement is idempotent — safe to re-run.
--
-- HOW TO RUN: Supabase → SQL Editor → "+ New query" (a FRESH tab) → paste
--             CONTENTS → Run. Expect "Success. No rows returned".
-- ═══════════════════════════════════════════════════════════════════════════


-- ───────────────────────────────────────────────────────────────────────────
-- 1. profiles.platforms — the platform preference (roadmap 7.1)
--
-- NULLABLE with a default. V1 actively SELECTs from profiles and does not know
-- this column; adding it nullable-with-default cannot break V1's reads, and V1's
-- writes (which never mention it) leave the default in place.
--
-- NULL is meaningful: "not yet asked". The API treats NULL as ['linkedin'] for
-- existing users rather than forcing everyone through onboarding again.
-- ───────────────────────────────────────────────────────────────────────────
alter table public.profiles
    add column if not exists platforms text[] default null;

comment on column public.profiles.platforms is
    'V2: which platforms the user optimizes for — subset of {linkedin,naukri}. '
    'NULL = never asked (treated as linkedin). Governs PROFILE OPTIMIZATION '
    'ONLY, never job sourcing — there is no compliant Naukri jobs access.';

-- Resume retention is an explicit opt-in (§21 Q5: analyze-and-discard default).
alter table public.profiles
    add column if not exists resume_retention_opt_in boolean default false;

comment on column public.profiles.resume_retention_opt_in is
    'V2: user explicitly opted in to KEEPING their uploaded resume file. '
    'Default false = analyze at upload, store findings, discard the file.';


-- ───────────────────────────────────────────────────────────────────────────
-- 2. naukri_scores — the Naukri track's own history
--
-- A SEPARATE table, not a column on score_history, because the two scores
-- measure genuinely different things (narrative positioning vs keyword/parse
-- coverage). Sharing a table invites someone to AVERAGE them later, which the
-- design forbids. Different table = the mistake is structurally harder to make.
-- ───────────────────────────────────────────────────────────────────────────
create table if not exists public.naukri_scores (
    id            bigint generated always as identity primary key,
    user_id       uuid not null references auth.users (id) on delete cascade,
    score         integer not null,
    dimensions    jsonb   not null default '{}'::jsonb,  -- the 7 weighted dimensions
    parse_issues  jsonb   not null default '[]'::jsonb,  -- detected structural problems
    cycle_state   text    not null default 'never_run',  -- never_run|needs_tuneup|optimized
    optimized_at  timestamptz,
    created_at    timestamptz not null default now()
);
create index if not exists naukri_scores_user_created_idx
    on public.naukri_scores (user_id, created_at desc);


-- ───────────────────────────────────────────────────────────────────────────
-- 3. thread_decisions — the Thread's PERSISTED half (§21 Q1: hybrid)
--
-- Candidates are DERIVED from live state on every request, so they can never go
-- stale. Only the user's DECISIONS live here. This is the whole point: if we
-- persisted "add Power BI to your Skills" and the user did it via Signal, a
-- persisted row would keep telling them to do something already done — a
-- credibility bug in a product whose pitch is "the system knows where you are".
--
-- item_key is a STABLE identity derived from the candidate's meaning
-- (e.g. 'score_fix:headline'), not a row id — the candidate is regenerated
-- each request and must match its own past decision.
-- ───────────────────────────────────────────────────────────────────────────
create table if not exists public.thread_decisions (
    id            bigint generated always as identity primary key,
    user_id       uuid not null references auth.users (id) on delete cascade,
    item_key      text not null,
    decision      text not null,           -- snoozed | declined | dismissed
    snooze_until  timestamptz,             -- set only when decision = 'snoozed'
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now(),
    unique (user_id, item_key)
);
create index if not exists thread_decisions_user_idx
    on public.thread_decisions (user_id, updated_at desc);


-- ───────────────────────────────────────────────────────────────────────────
-- 4. weekly_contracts — the Sunday ritual (§21 Q2)
--
-- 'auto_adopted' is a DISTINCT state from 'approved' on purpose. Brief 2's
-- lapsed grammar is: skip one Sunday → the standing plan auto-adopts; skip two
-- → the standing-by card. Recording an auto-adoption as 'approved' would make
-- the record claim the user agreed to something they never saw. Honesty applies
-- to our own data, not only to the UI.
-- ───────────────────────────────────────────────────────────────────────────
create table if not exists public.weekly_contracts (
    id            bigint generated always as identity primary key,
    user_id       uuid not null references auth.users (id) on delete cascade,
    week_start    date not null,
    state         text not null default 'proposed',
        -- proposed → approved | auto_adopted | declined | expired | lapsed
    items         jsonb not null default '[]'::jsonb,
    total_credits integer not null default 0,
    proposed_at   timestamptz not null default now(),
    decided_at    timestamptz,
    created_at    timestamptz not null default now(),
    unique (user_id, week_start)
);
create index if not exists weekly_contracts_user_week_idx
    on public.weekly_contracts (user_id, week_start desc);

-- Guard the state machine at the DB level so a bug in one code path can't
-- silently record an un-modelled state.
do $$
begin
    if not exists (
        select 1 from pg_constraint where conname = 'weekly_contracts_state_check'
    ) then
        alter table public.weekly_contracts
            add constraint weekly_contracts_state_check
            check (state in ('proposed','approved','auto_adopted','declined','expired','lapsed'));
    end if;
end $$;


-- ───────────────────────────────────────────────────────────────────────────
-- 5. market_digests — admin-curated market intelligence (roadmap 7.3)
--
-- Human-in-the-loop by design: an admin curates each weekly batch, so the
-- "real and sourced, never fabricated" guardrail is trivially enforceable.
-- Items carry a real citation; an item without one must not be publishable.
-- ───────────────────────────────────────────────────────────────────────────
create table if not exists public.market_digests (
    id            bigint generated always as identity primary key,
    batch_key     text not null unique,          -- e.g. 'mkt_2026_w32'
    items         jsonb not null default '[]'::jsonb,
    published_at  timestamptz,                   -- NULL = draft, not user-visible
    next_refresh  timestamptz,
    created_by    uuid references auth.users (id) on delete set null,
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now()
);
create index if not exists market_digests_published_idx
    on public.market_digests (published_at desc nulls last);


-- ───────────────────────────────────────────────────────────────────────────
-- 6. agent_runs — the run protocol's state (contract §3)
--
-- Replaces V1's in-process runtime.py thread registry, which could not survive
-- a restart and could not be polled from a second client.
--
-- 'empty' is a first-class terminal status, NOT a failure: the sources returned
-- nothing and nothing was fabricated. credits_charged stays 0 for it.
-- ───────────────────────────────────────────────────────────────────────────
create table if not exists public.agent_runs (
    id               uuid primary key default gen_random_uuid(),
    user_id          uuid not null references auth.users (id) on delete cascade,
    agent            text not null,               -- content|jobs|profile|interview|cv|naukri
    status           text not null default 'queued',
        -- queued | running | succeeded | empty | failed | cancelled
    steps            jsonb not null default '[]'::jsonb,
    result           jsonb,
    error            text,
    credits_charged  integer not null default 0,
    idempotency_key  text,
    provenance       jsonb not null default '{}'::jsonb,
    started_at       timestamptz not null default now(),
    finished_at      timestamptz
);
create index if not exists agent_runs_user_idx
    on public.agent_runs (user_id, started_at desc);
-- Replaying an Idempotency-Key must return the original run, never re-charge.
create unique index if not exists agent_runs_idempotency_idx
    on public.agent_runs (user_id, idempotency_key)
    where idempotency_key is not null;

do $$
begin
    if not exists (
        select 1 from pg_constraint where conname = 'agent_runs_status_check'
    ) then
        alter table public.agent_runs
            add constraint agent_runs_status_check
            check (status in ('queued','running','succeeded','empty','failed','cancelled'));
    end if;
end $$;


-- ───────────────────────────────────────────────────────────────────────────
-- 7. RLS
--
-- These are V2-only tables, so they get the CORRECT model from the start:
-- per-user policies keyed on auth.uid(). V2 sends a real per-request JWT, so
-- auth.uid() actually resolves — this is what Bible §26.11 asked for, and why
-- V2 does not inherit V1's permissive "app-server RLS" workaround.
--
-- The API additionally checks ownership in code (assert_owns); RLS is the
-- second lock, not the only one.
-- ───────────────────────────────────────────────────────────────────────────
alter table public.naukri_scores    enable row level security;
alter table public.thread_decisions enable row level security;
alter table public.weekly_contracts enable row level security;
alter table public.agent_runs       enable row level security;
alter table public.market_digests   enable row level security;

drop policy if exists "own naukri_scores" on public.naukri_scores;
create policy "own naukri_scores" on public.naukri_scores
    for all to authenticated using (auth.uid() = user_id) with check (auth.uid() = user_id);

drop policy if exists "own thread_decisions" on public.thread_decisions;
create policy "own thread_decisions" on public.thread_decisions
    for all to authenticated using (auth.uid() = user_id) with check (auth.uid() = user_id);

drop policy if exists "own weekly_contracts" on public.weekly_contracts;
create policy "own weekly_contracts" on public.weekly_contracts
    for all to authenticated using (auth.uid() = user_id) with check (auth.uid() = user_id);

drop policy if exists "own agent_runs" on public.agent_runs;
create policy "own agent_runs" on public.agent_runs
    for all to authenticated using (auth.uid() = user_id) with check (auth.uid() = user_id);

-- Market digests are curated centrally and READ by everyone; only the service
-- role writes them (admin authoring path).
drop policy if exists "read published market_digests" on public.market_digests;
create policy "read published market_digests" on public.market_digests
    for select to authenticated using (published_at is not null);


-- ═══════════════════════════════════════════════════════════════════════════
-- Done. Nothing above modifies or removes anything V1 depends on.
-- ═══════════════════════════════════════════════════════════════════════════
