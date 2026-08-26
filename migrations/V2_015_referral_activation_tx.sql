-- ═══════════════════════════════════════════════════════════════════════════
-- V2_015 · Referral activation becomes ONE transaction
--
-- ## The window this closes
--
-- `SupabaseReferralStore.activate` did three things over three round trips:
--
--   1. flip `referrals.state` pending -> activated (a conditional update, so
--      two concurrent uploads cannot both pay out — that part was right)
--   2. grant the referrer's credits
--   3. grant the referred user's credits
--
-- A failure after 1 leaves the row saying `activated` with nobody paid. A
-- failure after 2 pays one person. **And a retry cannot help**: `activate`
-- returns immediately when it sees `activated`, so the credits are owed with
-- no path to recovery. If step 2 succeeded and step 3 failed, one side is paid
-- and the other never will be.
--
-- The alternative — grant first, flip after — is worse: a crash between them
-- means a retry re-grants, which is the double-payment the payments path was
-- built to avoid.
--
-- So the window is removed rather than recovered from. Postgres already gives
-- us the primitive; the three round trips were the only reason it was not
-- being used.
--
-- ## Why SECURITY DEFINER is safe HERE and not for `referral_activate`
--
-- The other three referral functions are `security definer` and granted to
-- `authenticated`, because RLS on `referrals` otherwise makes the share
-- surface impossible. Activation must never join them: a function
-- `authenticated` can execute is one anybody can execute from a browser
-- console, and that would be a route to 70 credits without uploading anything.
--
-- The danger was `authenticated`, not `security definer`. This is granted to
-- **service_role only** — reachable from the server-side upload path that
-- already holds the service key, and from nowhere a browser can get to.
-- `execute` is revoked from `public`, `anon` and `authenticated` explicitly
-- rather than left to the default, because the default has changed before.
--
-- Additive: a new function, no table altered. Safe while V1 is live.
-- ═══════════════════════════════════════════════════════════════════════════

create or replace function public.referral_activate_tx(
    p_referred_id       uuid,
    p_at                timestamptz,
    p_referrer_credits  integer,
    p_referred_credits  integer,
    p_referrer_kind     text,
    p_referred_kind     text
)
returns table (referrer_id uuid, slot integer, referrer_balance integer, referred_balance integer)
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    v_referrer uuid;
    v_slot     integer;
    v_ref_bal  integer;
    v_new_bal  integer;
begin
    -- The claim. `where state = 'pending'` makes this the same conditional
    -- update as before: two concurrent callers, one winner. RETURNING tells us
    -- whether THIS caller won, which is the only safe way to ask.
    update public.referrals
       set state = 'activated',
           activated_at = p_at
     where referred_id = p_referred_id
       and state = 'pending'
    returning referrals.referrer_id, referrals.slot
      into v_referrer, v_slot;

    if v_referrer is null then
        -- No pending referral, or somebody else just activated it. Not an
        -- error: most users were never referred.
        return;
    end if;

    -- Everything below is in the SAME transaction as the flip above. There is
    -- no longer a state in which the row says `activated` and the credits do
    -- not exist — either both happen or neither does.

    insert into public.credits (user_id, balance, updated_at)
         values (v_referrer, p_referrer_credits, p_at)
    on conflict (user_id) do update
            set balance = public.credits.balance + p_referrer_credits,
                updated_at = p_at
      returning balance into v_ref_bal;

    insert into public.credit_ledger (user_id, delta, kind, reason, balance_after, created_at)
         values (v_referrer, p_referrer_credits, p_referrer_kind,
                 'Referral activated (' || left(p_referred_id::text, 8) || ')',
                 v_ref_bal, p_at);

    insert into public.credits (user_id, balance, updated_at)
         values (p_referred_id, p_referred_credits, p_at)
    on conflict (user_id) do update
            set balance = public.credits.balance + p_referred_credits,
                updated_at = p_at
      returning balance into v_new_bal;

    insert into public.credit_ledger (user_id, delta, kind, reason, balance_after, created_at)
         values (p_referred_id, p_referred_credits, p_referred_kind,
                 'Welcome bonus from a referral', v_new_bal, p_at);

    return query select v_referrer, v_slot, v_ref_bal, v_new_bal;
end $$;

-- Explicit, in this order. `revoke from public` first, because a grant to
-- `public` is inherited by every role and would make the grants below
-- meaningless.
revoke all on function public.referral_activate_tx(uuid, timestamptz, integer, integer, text, text)
    from public;
revoke all on function public.referral_activate_tx(uuid, timestamptz, integer, integer, text, text)
    from anon;
revoke all on function public.referral_activate_tx(uuid, timestamptz, integer, integer, text, text)
    from authenticated;
grant execute on function public.referral_activate_tx(uuid, timestamptz, integer, integer, text, text)
    to service_role;

comment on function public.referral_activate_tx is
    'Activation and both credit grants in ONE transaction. service_role only: a '
    'version authenticated could execute would be a browser-callable route to 70 '
    'credits. Replaces a three-round-trip sequence whose failure left a referral '
    'marked activated with the credits ungranted and no way to retry.';
