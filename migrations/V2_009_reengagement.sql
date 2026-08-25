-- ───────────────────────────────────────────────────────────────────────────
-- V2_009 — reengagement_sequences: the lapsed-user digest, with a real ending
--
-- One ROW PER SEQUENCE, not per user. A user who lapses, gets some sends,
-- returns, and lapses again has two rows — which is both the correct model and
-- a free lapse-and-return history for admin.
--
-- ## Why `state` is written, not derived
--
-- The obvious implementation counts sends and compares against the cadence
-- table: eight sent, so they are finished. That is a comparison against
-- something that can CHANGE. Add a fourth weekly phase later and every user
-- who had finished at eight is silently below the new total — resurrected,
-- months after they stopped hearing from us, by a config edit nobody connected
-- to them.
--
-- So `finished` is written at the moment the last send completes and never
-- recomputed. A finished sequence is a fact about what happened; a count is an
-- opinion about a table.
--
-- Same family as `cannot count ≠ counted zero`: the states that must never be
-- confused are given different representations rather than different numbers.
--
-- ## The states
--
--   active     receiving. Exactly one per user may be active.
--   returned   they signed in. Closed, not deleted — this is the history.
--   finished   all sends made, never came back. Terminal, permanently.
--
-- Only `active` is ever selected for sending, so neither closed state can be
-- reopened by any change to the cadence.
-- ───────────────────────────────────────────────────────────────────────────

create table if not exists public.reengagement_sequences (
    id            bigint generated always as identity primary key,
    user_id       uuid not null references auth.users (id) on delete cascade,

    started_at    timestamptz not null default now(),
    --: How many digests this sequence has actually SENT. Used to pick the next
    --: interval, never to decide whether the sequence is over.
    sends_made    integer not null default 0,
    last_sent_at  timestamptz,
    --: When the next one is due. NULL means "not scheduled" — a sequence that
    --: is active but unscheduled is a bug, and the cron reports it rather than
    --: guessing a date.
    next_due_at   timestamptz,

    state         text not null default 'active',
    closed_at     timestamptz,

    created_at    timestamptz not null default now(),

    constraint reengagement_state_ck
        check (state in ('active', 'returned', 'finished')),

    -- A closed sequence has a closing time, and an open one does not. One
    -- fact, so they cannot disagree.
    constraint reengagement_closed_together_ck
        check ((state = 'active') = (closed_at is null)),

    constraint reengagement_sends_ck
        check (sends_made >= 0)
);

-- At most ONE active sequence per user. Enforced by the database rather than
-- by the cron remembering to check: two active sequences would double every
-- send, and the duplicate-send guard in digest_log is per-day, not per-user.
create unique index if not exists reengagement_one_active_per_user
    on public.reengagement_sequences (user_id)
    where state = 'active';

create index if not exists reengagement_due_idx
    on public.reengagement_sequences (next_due_at)
    where state = 'active';

create index if not exists reengagement_user_idx
    on public.reengagement_sequences (user_id, started_at desc);


-- ───────────────────────────────────────────────────────────────────────────
-- RLS from birth. SERVICE-ROLE ONLY, so no policies.
--
-- Nothing user-facing reads or writes this: the cron decides who is due, and
-- admin reads the history. Same shape as email_log and digest_log, and for the
-- same reason — a table with no user-facing path gets RLS with no policy, and
-- gets it on day one rather than in a remediation migration.
--
-- carigma:rls-service-role-only — declared rather than left to be inferred.
-- RLS with no policy denies everyone, which is correct here and a silent
-- outage anywhere else; the two are indistinguishable from the SQL alone, so
-- the guard in tests/test_migrations.py requires the intent in writing. It
-- caught this file before I added the marker, which is the guard working.
-- ───────────────────────────────────────────────────────────────────────────
alter table public.reengagement_sequences enable row level security;


-- ───────────────────────────────────────────────────────────────────────────
-- Verification — expect rls_enabled = true and policies = 0.
-- ───────────────────────────────────────────────────────────────────────────
select
    c.relname        as table_name,
    c.relrowsecurity as rls_enabled,
    (select count(*)
       from pg_policies p
      where p.schemaname = 'public' and p.tablename = c.relname) as policy_count
  from pg_class c
  join pg_namespace n on n.oid = c.relnamespace
 where n.nspname = 'public'
   and c.relname = 'reengagement_sequences';

-- Then, from carigma-api:  python scripts/verify_rls.py
-- The table is picked up automatically — the probe derives its list from
-- PostgREST rather than a hand-maintained tuple.
