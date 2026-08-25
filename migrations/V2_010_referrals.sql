-- ───────────────────────────────────────────────────────────────────────────
-- V2_010 — referral attribution: codes now, promotion later
--
-- If we launch without codes and add them in three months, we can never
-- attribute the first hundred users and will never learn which channel worked.
-- Cheap now, impossible to retrofit.
--
-- ## The cap is a set of SLOTS, not a count
--
-- The obvious implementation reads `count(*) where referrer_id = X`, compares
-- to 5, and inserts. That is read-then-write: under READ COMMITTED two
-- concurrent activations both see 4 and both insert, and the referrer has six.
-- Exactly the shape of V1's double-credit bug.
--
-- So there are five NUMBERED slots per referrer, and claiming one is an insert
-- that either wins the unique index or loses it. The database arbitrates; the
-- application retries the next slot. No count is ever trusted.
--
--     unique (referrer_id, slot)      two claims on one slot -> one wins
--     check (slot between 1 and 5)    six is not expressible
--
-- The cap is therefore a property of the schema rather than a branch someone
-- could forget, reorder, or race.
--
-- ## Self-referral is structural
--
--     check (referrer_id <> referred_id)
--
-- Not a validation the API performs — a row the database will not hold. The
-- alternative is discovering it as a credit drain.
--
-- ## One referral per referred user
--
--     unique (referred_id)
--
-- Someone can be referred once, ever. Without this, a user could be "activated"
-- against several referrers and mint credits for each.
-- ───────────────────────────────────────────────────────────────────────────

-- ── Codes ──────────────────────────────────────────────────────────────────

create table if not exists public.referral_codes (
    user_id    uuid primary key references auth.users (id) on delete cascade,
    code       text not null unique,
    created_at timestamptz not null default now(),

    -- Long enough not to be guessable by hand, short enough to read aloud.
    constraint referral_codes_shape_ck check (code ~ '^[a-z0-9]{6,12}$')
);


-- ── Attributions ───────────────────────────────────────────────────────────

create table if not exists public.referrals (
    id           bigint generated always as identity primary key,
    referrer_id  uuid not null references auth.users (id) on delete cascade,
    referred_id  uuid not null references auth.users (id) on delete cascade,
    code         text not null,

    --: Which of the referrer's five slots this occupies. THE cap mechanism.
    slot         integer not null,

    --: `pending` from signup; `activated` only once the referred user has
    --: actually uploaded a profile. Credits move on the second, never the first.
    state        text not null default 'pending',
    activated_at timestamptz,

    created_at   timestamptz not null default now(),

    constraint referrals_state_ck
        check (state in ('pending', 'activated')),

    -- A state and its timestamp are one fact.
    constraint referrals_activated_together_ck
        check ((state = 'activated') = (activated_at is not null)),

    -- Structural, not validated: the database will not hold a self-referral.
    constraint referrals_no_self_ck
        check (referrer_id <> referred_id),

    -- Five slots. Six is not expressible.
    constraint referrals_slot_range_ck
        check (slot between 1 and 5),

    -- Someone can be referred exactly once, ever.
    constraint referrals_one_per_referred_uq
        unique (referred_id)
);

-- THE cap. Two concurrent claims on the same slot: one wins, one gets 23505
-- and retries the next. There is no count anywhere in that sentence.
create unique index if not exists referrals_slot_uq
    on public.referrals (referrer_id, slot);

create index if not exists referrals_referrer_idx
    on public.referrals (referrer_id, created_at desc);


-- ── Ledger vocabulary ──────────────────────────────────────────────────────
--
-- Two new kinds, not one. The referrer's 50 and the referred user's 20 are
-- different events with different meanings, and collapsing them would make
-- "what did referrals cost us" unanswerable without parsing reason strings.
--
-- ## Dropping this constraint is permitted, and the guard checks why
--
-- The additive-only rule protects V1. `credit_ledger_kind_check` was created
-- by V2_002 — it is not V1's, and widening a vocabulary V2 owns is how that
-- vocabulary grows. The precedent cuts the other way too: in P5 the same guard
-- caught an attempt to widen V1's `email_log.status`, and that one was
-- correctly abandoned in favour of a new column.
--
-- The marker below is verified, not trusted: `test_migrations.py` requires the
-- named constraint to appear in an earlier migration's `add constraint`. A
-- claim of ownership that nothing checks would be the vacuous-guard pattern
-- wearing an opt-out.
--
-- carigma:v2-owned-constraint: credit_ledger_kind_check
-- ───────────────────────────────────────────────────────────────────────────

alter table public.credit_ledger
    drop constraint if exists credit_ledger_kind_check;

alter table public.credit_ledger
    add constraint credit_ledger_kind_check
    -- `null` stays permitted so pre-existing V1 rows remain valid; readers
    -- treat it as 'spend'.
    check (kind is null or kind in (
        'spend', 'signup', 'grant', 'purchase', 'admin_adjustment', 'refund',
        --: Paid to the REFERRER on the referred user's activation.
        'referral',
        --: Paid to the REFERRED user, so the link is a gift given rather than
        --: a favour asked. Distinct kind, so the two sides stay legible.
        'referral_bonus'
    ));


-- ───────────────────────────────────────────────────────────────────────────
-- RLS from birth.
--
-- `referral_codes` is USER-FACING: someone reads their own code to share it.
-- Owner-scoped on both clauses.
--
-- `referrals` is NOT. Attribution is written by the activation path under the
-- service role and read by admin; a user has no reason to enumerate who they
-- referred, and letting them read rows keyed on another person's user_id would
-- leak that association.
--
-- carigma:rls-service-role-only — for `referrals` only. Declared because RLS
-- with no policy denies everyone, which is correct here and a silent outage
-- anywhere else, and the two are indistinguishable from the SQL alone.
-- ───────────────────────────────────────────────────────────────────────────

alter table public.referral_codes enable row level security;

drop policy if exists referral_codes_own on public.referral_codes;
create policy referral_codes_own on public.referral_codes
    as permissive for all to authenticated
    using (auth.uid() = user_id)
    with check (auth.uid() = user_id);

alter table public.referrals enable row level security;


-- ───────────────────────────────────────────────────────────────────────────
-- Verification
--
-- Expect: referral_codes rls=true policies=1 (both clauses set)
--         referrals      rls=true policies=0
-- ───────────────────────────────────────────────────────────────────────────
select
    c.relname        as table_name,
    c.relrowsecurity as rls_enabled,
    (select count(*) from pg_policies p
      where p.schemaname = 'public' and p.tablename = c.relname) as policy_count
  from pg_class c
  join pg_namespace n on n.oid = c.relnamespace
 where n.nspname = 'public'
   and c.relname in ('referral_codes', 'referrals')
 order by c.relname;

-- And the cap, provable by hand:
--   insert six rows for one referrer -> the sixth fails on
--   referrals_slot_range_ck or referrals_slot_uq, never succeeds.
