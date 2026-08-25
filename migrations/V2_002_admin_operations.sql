-- ───────────────────────────────────────────────────────────────────────────
-- V2_002 — Admin operations (P5 Part C)
--
-- STRICTLY ADDITIVE, like V2_001. Nothing here drops, renames or alters an
-- existing column, so live V1 continues to run untouched against the same
-- database.
--
-- Two tables, both existing because top-ups are MANUAL during beta and admin
-- is therefore an operational tool rather than a reporting dashboard.
-- ───────────────────────────────────────────────────────────────────────────


-- ───────────────────────────────────────────────────────────────────────────
-- 1. credit_requests — the willingness-to-pay signal
--
-- When someone ASKS for more credits, that is the single most valuable datum
-- in the beta: those are the people who would pay. Today that signal arrives
-- as a WhatsApp message and dies there.
--
-- Modelled as a request with a lifecycle rather than a boolean flag on the
-- user, because "asked three times over six weeks" and "asked once" are very
-- different signals and a flag cannot tell them apart.
-- ───────────────────────────────────────────────────────────────────────────
create table if not exists public.credit_requests (
    id              bigint generated always as identity primary key,
    user_id         uuid not null references auth.users (id) on delete cascade,
    asked_at        timestamptz not null default now(),
    -- Where it came from, so the founder can tell an in-app ask (deliberate,
    -- high intent) from a passing WhatsApp remark.
    channel         text not null default 'in_app',
    -- The user's own words, kept verbatim. This is qualitative research data,
    -- not a form field to be normalised away.
    note            text,
    resolved_at     timestamptz,
    resolved_by     text,
    credits_granted integer,
    created_at      timestamptz not null default now()
);

create index if not exists credit_requests_open_idx
    on public.credit_requests (asked_at desc)
    where resolved_at is null;

create index if not exists credit_requests_user_idx
    on public.credit_requests (user_id, asked_at desc);

do $$
begin
    if not exists (
        select 1 from pg_constraint where conname = 'credit_requests_channel_check'
    ) then
        alter table public.credit_requests
            add constraint credit_requests_channel_check
            check (channel in ('in_app', 'whatsapp', 'email', 'other'));
    end if;
end $$;

comment on table public.credit_requests is
    'Willingness-to-pay signal: a user asking for more credits. The highest-value '
    'datum of the beta — these are the people who would pay.';


-- ───────────────────────────────────────────────────────────────────────────
-- 2. admin_notes — free-text context on a user
--
-- Separate from credit_requests because not every note is an ask. "Recruiter,
-- not a job seeker" is worth remembering and is not a purchase signal.
-- ───────────────────────────────────────────────────────────────────────────
create table if not exists public.admin_notes (
    id         bigint generated always as identity primary key,
    user_id    uuid not null references auth.users (id) on delete cascade,
    note       text not null,
    author     text not null,
    created_at timestamptz not null default now()
);

create index if not exists admin_notes_user_idx
    on public.admin_notes (user_id, created_at desc);


-- ───────────────────────────────────────────────────────────────────────────
-- 3. credit_ledger.kind — make a manual adjustment distinguishable
--
-- The ledger already records delta + reason. What it cannot currently answer
-- is "how many credits did we GIVE AWAY versus SELL versus grant at signup?"
-- — and during a manual-top-up beta that is exactly the number that matters.
--
-- Defaulted to 'spend' so every existing row keeps a valid value without a
-- backfill; positive-delta history is corrected by the update below, which is
-- idempotent and safe to re-run.
-- ───────────────────────────────────────────────────────────────────────────
-- NULLABLE on purpose. A `not null default 'spend'` would in fact be safe —
-- Postgres fills the default for existing rows and for inserts that omit the
-- column, so live V1 writes would not break. But the additive-migration guard
-- rejects NOT NULL on a V1 table on principle, and the principle is worth more
-- than the constraint: readers below treat a null `kind` as 'spend', which is
-- what every pre-existing negative row is.
alter table public.credit_ledger
    add column if not exists kind text default 'spend';

-- Existing positive deltas were grants of some sort; the reason text is the
-- only evidence of which. Signup grants are identifiable; everything else
-- positive becomes 'grant' rather than being guessed at more precisely.
update public.credit_ledger
   set kind = case
       when delta > 0 and reason ilike '%signup%'  then 'signup'
       when delta > 0 and reason ilike '%top-up%'  then 'purchase'
       when delta > 0                              then 'grant'
       else 'spend'
   end
 where kind = 'spend' and delta > 0;

do $$
begin
    if not exists (
        select 1 from pg_constraint where conname = 'credit_ledger_kind_check'
    ) then
        alter table public.credit_ledger
            add constraint credit_ledger_kind_check
            -- `null` is permitted so pre-existing V1 rows stay valid; readers
            -- treat it as 'spend'.
            check (kind is null or kind in (
                'spend', 'signup', 'grant', 'purchase', 'admin_adjustment', 'refund'
            ));
    end if;
end $$;

comment on column public.credit_ledger.kind is
    'What kind of movement this was. admin_adjustment is a manual founder '
    'top-up and is deliberately distinct from purchase and signup, so '
    '"credits issued" can be read honestly.';


-- ───────────────────────────────────────────────────────────────────────────
-- 4. email_log — widen it, do NOT recreate it
--
-- CORRECTION. This section originally did `create table if not exists
-- email_log (...)` with a shape of my own invention. Both `email_log` and
-- `digest_log` ALREADY EXIST from V1, so `if not exists` silently did nothing
-- and the cron then failed against the real database with "Could not find the
-- 'detail' column of 'email_log'". Found by running the cron for real.
--
-- V1's shape (resumeiq/db.py:log_email):
--   user_id · recipient · email_type · subject · status · error
--
-- So: add only what is missing, and widen the status vocabulary. V1 writes
-- only 'sent' and 'failed'; V2 also needs 'skipped' (nothing to say — a
-- success), 'dry_run' and 'unsubscribed'.
-- ───────────────────────────────────────────────────────────────────────────
alter table public.email_log
    add column if not exists detail text;

comment on column public.email_log.detail is
    'V2 addition. V1 used `error` for failures only; `detail` also carries the '
    'reason for a skip, which is not an error.';

-- V2 needs a richer vocabulary than V1's sent|failed: 'skipped' (nothing to
-- say — a SUCCESS), 'dry_run', 'unsubscribed'. An earlier draft dropped and
-- recreated V1's status check to widen it; the additive-migration guard
-- correctly flagged that `drop constraint` as destructive, and it was — a
-- migration that drops something a live app depends on is exactly what the
-- guard exists to stop, superset or not.
--
-- So V1's `status` is left completely alone and the true outcome goes in a NEW
-- column. Readers prefer `outcome` and fall back to `status`.
alter table public.email_log
    add column if not exists outcome text;

comment on column public.email_log.outcome is
    'V2 outcome vocabulary: sent | failed | skipped | dry_run | unsubscribed | '
    'duplicate. Distinct from V1 `status` (sent|failed), which is untouched. '
    'A skip is a SUCCESS — nothing real happened, so no email was sent.';


-- ───────────────────────────────────────────────────────────────────────────
-- 5. The duplicate-send guard ALREADY EXISTS — use it
--
-- `digest_log` has `unique (user_id, digest_type, sent_on)`, which is exactly
-- the guard V2 needs: one email of each type per user per period, enforced by
-- the database so two racing cron processes cannot both send.
--
-- An earlier draft of this migration created a second table (`email_sends`)
-- doing the same job. Two guards over one invariant is the failure mode we
-- rejected for rate limiting in P4 — they duplicate or they disagree. Removed;
-- V2 writes to `digest_log`, with `period_key` stored in `sent_on`.
-- ───────────────────────────────────────────────────────────────────────────


-- ═══════════════════════════════════════════════════════════════════════════
-- ROW LEVEL SECURITY
--
-- CORRECTION — this was missing, and Supabase's linter caught it before our
-- own migration guard did. V2_001 enabled RLS on every table it created and
-- gave each one an `auth.uid() = user_id` policy. V2_002 created two tables
-- and did neither. That is a real exposure, not a theoretical one:
--
--   • `admin_notes` holds the founder's private observations about users
--     ("Recruiter, not a job seeker"). No user should ever read that — least
--     of all the user it is written about.
--   • `credit_requests` holds users' verbatim asks. One user's ask is not
--     another user's to browse.
--
-- Both tables are ADMIN-ONLY, and every code path reaches them through the
-- SERVICE ROLE, which bypasses RLS entirely. So the correct state is
-- **RLS enabled with NO policies**: `anon` and `authenticated` can do nothing
-- at all, and admin is unaffected.
--
-- carigma:rls-service-role-only — declared deliberately. RLS with no policy
-- returns an EMPTY SET rather than an error, so it is indistinguishable from a
-- forgotten policy unless someone says which it is. This is the saying.
--
-- Verified before writing this — every reference to either table in
-- `routes/admin.py` goes through `service_client`, including the user-facing
-- `POST /credits/request`. That endpoint stays safe because it writes
-- `user_id = user.id` from the VERIFIED TOKEN and never from the request body:
-- its protection is the route, not RLS, so enabling RLS takes nothing away.
--
-- IF YOU ADD A FEATURE HERE: there is no user-facing READ of either table
-- today. If you ever want to show someone "you asked for credits — we're on
-- it", that needs a policy:
--
--     create policy "own credit_requests" on public.credit_requests
--         for select to authenticated using (auth.uid() = user_id);
--
-- Without one, RLS returns an EMPTY SET rather than an error — so the feature
-- would look like it worked and quietly show nothing.
-- ═══════════════════════════════════════════════════════════════════════════
alter table public.credit_requests enable row level security;
alter table public.admin_notes     enable row level security;

comment on table public.admin_notes is
    'Founder-private notes about users. RLS enabled with NO policies on '
    'purpose — service-role only. Never expose to anon or authenticated.';
