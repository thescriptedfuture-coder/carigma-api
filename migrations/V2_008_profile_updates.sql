-- ───────────────────────────────────────────────────────────────────────────
-- V2_008 — profile_updates: the incremental change loop
--
-- There is a RE-UPLOAD path (Settings → LinkedIn sync, Naukri tune-up step 6),
-- but it replaces the whole document. There is no path for "I earned a
-- certification — add it properly". Different mechanics; this is the missing
-- one.
--
-- The row exists because the flow is detect → PROPOSE → approve, and the
-- proposal has to survive the user navigating away. It is not a log of what
-- happened; it is the pending offer and the record of what they agreed to.
--
-- ## `state` carries the same discipline as the weekly contract
--
-- `approved` means the user actually said yes. There is deliberately NO
-- auto-adopt value: a profile rewrite the user never saw is exactly the thing
-- the co-pilot model forbids, and V2's weekly contract already paid for that
-- lesson once. `decided_at` is NULL until a human decides, and the check
-- constraint below makes the pair inseparable.
--
-- ## Additive
--
-- New table only. Nothing V1 reads is touched, and V1 has no concept of this.
-- ───────────────────────────────────────────────────────────────────────────

create table if not exists public.profile_updates (
    id          bigint generated always as identity primary key,
    user_id     uuid not null references auth.users (id) on delete cascade,

    -- What changed. Structured, not free text, because every downstream agent
    -- reads this and "Python (advanced), 2026" is usable where a sentence is not.
    kind        text not null,
    operation   text not null default 'add',
    payload     jsonb not null default '{}'::jsonb,

    -- How big a deal it is. A job change treated like a skill addition would
    -- feel broken; so would a certification triggering a full re-onboarding.
    magnitude   text not null,

    -- What we offered to do about it, and where that offer stands.
    proposal    jsonb not null default '{}'::jsonb,
    state       text not null default 'proposed',
    decided_at  timestamptz,
    applied_at  timestamptz,

    created_at  timestamptz not null default now(),

    constraint profile_updates_kind_ck
        check (kind in ('skill', 'certification', 'project', 'role', 'education')),
    constraint profile_updates_operation_ck
        check (operation in ('add', 'remove')),
    constraint profile_updates_magnitude_ck
        check (magnitude in ('quiet', 'notable', 'major')),
    constraint profile_updates_state_ck
        check (state in ('proposed', 'approved', 'declined', 'applied')),

    -- A decision and its timestamp are one fact. Splitting them is how a row
    -- ends up claiming the user approved something with no record of when —
    -- which is indistinguishable from us deciding on their behalf.
    constraint profile_updates_decided_together_ck
        check ((state = 'proposed') = (decided_at is null)),

    -- Applied implies approved. Nothing reaches the user's live profile
    -- without having passed through a yes.
    constraint profile_updates_applied_after_approval_ck
        check (applied_at is null or state = 'applied')
);

create index if not exists profile_updates_user_idx
    on public.profile_updates (user_id, created_at desc);

-- The open proposal a surface needs to render. Partial, because 'proposed' is
-- the only state anyone queries by.
create index if not exists profile_updates_open_idx
    on public.profile_updates (user_id)
    where state = 'proposed';


-- ───────────────────────────────────────────────────────────────────────────
-- RLS from birth — user-facing, so it gets a policy.
--
-- The V2_007 rule applied at creation rather than retrofitted: a table that is
-- read and written by the user in their own session gets
-- `auth.uid() = user_id` on both sides. WITH CHECK matters as much as USING —
-- without it a user could write a row owned by someone else while only being
-- able to read their own.
-- ───────────────────────────────────────────────────────────────────────────
alter table public.profile_updates enable row level security;

drop policy if exists profile_updates_own on public.profile_updates;
create policy profile_updates_own on public.profile_updates
    as permissive for all to authenticated
    using (auth.uid() = user_id)
    with check (auth.uid() = user_id);


-- ───────────────────────────────────────────────────────────────────────────
-- Verification — expect one row, both clauses set, rls_enabled = true.
-- ───────────────────────────────────────────────────────────────────────────
select
    c.relname            as table_name,
    c.relrowsecurity     as rls_enabled,
    p.policyname,
    p.roles,
    p.qual,
    p.with_check
  from pg_class c
  join pg_namespace n on n.oid = c.relnamespace
  left join pg_policies p
    on p.schemaname = 'public' and p.tablename = c.relname
 where n.nspname = 'public'
   and c.relname = 'profile_updates';

-- Then, from carigma-api:
--   python scripts/verify_rls.py             (picks the table up automatically)
--   python scripts/verify_rls_crossuser.py   (add it to OWNER_SCOPED)
