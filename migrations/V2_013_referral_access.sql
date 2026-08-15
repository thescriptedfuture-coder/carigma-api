-- ───────────────────────────────────────────────────────────────────────────
-- V2_013 — reaching the referral tables without a service key
--
-- V2_010 shipped `referrals` with RLS on and NO policy, deliberately: a user
-- has no reason to enumerate who they referred, and rows keyed on another
-- person's user_id would leak that association. `referral_codes` is
-- owner-scoped for the same reason.
--
-- Both are right, and together they make the referral SURFACE impossible:
--
--   * `GET /referrals` reads `referrals` through the caller's client and gets
--     nothing back. Not an error — an empty list. Every referrer would be told
--     "0 activated, 5 slots left" forever, which is worse than a failure
--     because it looks like an answer.
--   * `POST /referrals/claim` inserts into `referrals` and is denied.
--   * `GET /referrals/{code}` has no session at all — the visitor has not
--     signed up yet, which is the entire point of the link.
--
-- ## Why not the service key
--
-- The obvious fix is a service-role client in the route. The standing rule is
-- that `SUPABASE_SERVICE_KEY` appears only in admin and cron paths, and it is
-- a good rule: a service-role client in a user-facing handler is one missing
-- `.eq("user_id", ...)` away from serving somebody else's data, and no test
-- catches the missing filter because the query still succeeds.
--
-- ## Why not a policy
--
-- `using (referrer_id = auth.uid())` would work and would expose MORE than the
-- endpoint does. PostgREST is a general query interface: a user with a policy
-- to read their referral rows can read the `referred_id` column, which is the
-- association V2_010 closed the table to protect. The endpoint returns counts.
--
-- ## SECURITY DEFINER, returning only what the surface needs
--
-- Three functions. Each runs as the owner, each derives the user from
-- `auth.uid()` rather than an argument, and each returns the narrowest thing
-- that answers the question. There is no argument any of them can be called
-- with to get another person's data, because the identity is not an argument.
--
-- `search_path` is pinned on all three. A SECURITY DEFINER function without it
-- resolves unqualified names against the CALLER's path, which is a textbook
-- privilege-escalation route.
-- ───────────────────────────────────────────────────────────────────────────

-- ── The referrer's own summary: counts, never ids ──────────────────────────

create or replace function public.referral_summary()
returns table (activated integer, pending integer, total integer)
language sql
security definer
set search_path = public, pg_temp
stable
as $$
    select
        count(*) filter (where state = 'activated')::integer,
        count(*) filter (where state = 'pending')::integer,
        count(*)::integer
    from public.referrals
    where referrer_id = auth.uid();
$$;

revoke all on function public.referral_summary() from public;
grant execute on function public.referral_summary() to authenticated;


-- ── Claiming: the insert, with the cap still owned by the index ────────────

create or replace function public.referral_claim(p_code text)
returns table (slot integer, state text)
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    v_referrer uuid;
    v_me       uuid := auth.uid();
    v_slot     integer;
begin
    if v_me is null then
        raise exception 'not signed in' using errcode = '42501';
    end if;

    select user_id into v_referrer from public.referral_codes where code = p_code;
    if v_referrer is null then
        raise exception 'unknown code' using errcode = 'P0002';
    end if;

    -- The database already refuses this via `referrals_no_self_ck`. Raising
    -- here makes it a sentence rather than a constraint violation, and the
    -- check below is not the guarantee.
    if v_referrer = v_me then
        raise exception 'self referral' using errcode = 'P0001';
    end if;

    -- Try each slot in turn and let the UNIQUE INDEX decide. This is the cap.
    -- Nothing here counts rows and compares to five: two concurrent callers
    -- both reading four is exactly the bug the slots design exists to prevent,
    -- so the loser of an insert retries rather than trusting what it read.
    for v_slot in 1..5 loop
        begin
            insert into public.referrals (referrer_id, referred_id, code, slot, state)
            values (v_referrer, v_me, p_code, v_slot, 'pending');
            return query select v_slot, 'pending'::text;
            return;
        exception when unique_violation then
            -- Either the slot or `referrals_one_per_referred_uq`. Only the
            -- first is worth retrying; being referred twice is final.
            if exists (select 1 from public.referrals where referred_id = v_me) then
                raise exception 'already referred' using errcode = 'P0003';
            end if;
        end;
    end loop;

    raise exception 'cap reached' using errcode = 'P0004';
end;
$$;

revoke all on function public.referral_claim(text) from public;
grant execute on function public.referral_claim(text) to authenticated;


-- ── The landing page: does this code exist, and nothing else ───────────────

create or replace function public.referral_code_valid(p_code text)
returns boolean
language sql
security definer
set search_path = public, pg_temp
stable
as $$
    select exists (select 1 from public.referral_codes where code = p_code);
$$;

-- `anon` on purpose: the visitor has no account, which is what the link is
-- for. It returns a BOOLEAN — no user id, no name, no email. Someone brute
-- forcing codes learns only which random eight-character strings exist, and
-- the route in front of it is rate limited per IP.
revoke all on function public.referral_code_valid(text) from public;
grant execute on function public.referral_code_valid(text) to anon, authenticated;


-- ── Minting a code stays in RLS, where it already works ────────────────────
--
-- `referral_codes` is owner-scoped for `all`, so a user reading and inserting
-- their OWN code needs no function. Nothing is added for it here: a
-- SECURITY DEFINER wrapper around a query RLS already permits would be a
-- second path to the same rows, and the second path is the one that gets its
-- filter wrong.


-- ───────────────────────────────────────────────────────────────────────────
-- Verification
--
-- Expect three rows, all `security definer` true and `search_path` pinned.
-- ───────────────────────────────────────────────────────────────────────────
select
    p.proname                                as function_name,
    p.prosecdef                              as security_definer,
    coalesce(array_to_string(p.proconfig, ', '), '(none)') as settings
from pg_proc p
join pg_namespace n on n.oid = p.pronamespace
where n.nspname = 'public'
  and p.proname in ('referral_summary', 'referral_claim', 'referral_code_valid')
order by p.proname;
