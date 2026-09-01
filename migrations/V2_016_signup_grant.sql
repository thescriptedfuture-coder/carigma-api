-- ═══════════════════════════════════════════════════════════════════════════
-- V2_016 · The welcome grant, once per user, in one transaction
--
-- ## What was missing
--
-- `SIGNUP_CREDITS = 100` was defined in `services/constants.py` and read by
-- **nothing**. A whole feature, declared and never called — the ninth instance
-- of the orphan class in this project.
--
-- Underneath it, two things made the grant impossible even if something had
-- called it:
--
--   * `apply_delta` wrote with `.update()`. An UPDATE matching no rows is a
--     SUCCESS in PostgREST — 200, nothing changed — so every credit write for
--     a user with no `credits` row silently did nothing.
--   * `grant()` returned "did not happen" when the balance came back NULL, and
--     NULL is what a missing row looks like. Granting required a balance to
--     add to, and nothing created the first one.
--
-- So a brand-new account could not receive credits by any path.
--
-- ## Why a function rather than two client calls
--
-- The same reason as V2_015. Two round trips — insert the ledger row, then
-- update the balance — have a window between them, and a failure inside that
-- window leaves the ledger claiming a grant that the balance does not show.
-- One transaction has no window.
--
-- ## Once, and provably once
--
-- `credit_ledger_one_signup_grant` is a PARTIAL unique index: one row per user
-- with `kind = 'signup'`, and no constraint at all on the other kinds. The
-- ledger insert happens FIRST, so a second call violates the index and the
-- whole transaction — grant included — rolls back.
--
-- **The database decides, not the caller.** A `select ... if not exists` in
-- Python would be read-then-write, and two uploads arriving together would
-- both read "not granted" and both pay out. Same rule the referral cap follows.
--
-- ## Grants attach to ACTIVATION, never to registration
--
-- Called when a profile is completed, not when an account is created — the
-- anti-farming rule the referral programme already follows. An account costs
-- an email address; a completed profile costs real work.
--
-- Additive: one index, one function. Safe while V1 is live.
-- ═══════════════════════════════════════════════════════════════════════════

create unique index if not exists credit_ledger_one_signup_grant
    on public.credit_ledger (user_id)
    where kind = 'signup';

create or replace function public.grant_signup_credits(
    p_user_id uuid,
    p_amount  integer,
    p_at      timestamptz
)
returns integer
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    v_balance integer;
begin
    if p_amount <= 0 then
        raise exception 'refusing to grant % credits', p_amount;
    end if;

    -- FIRST, so the unique index is what decides. If this user already has a
    -- signup grant, the insert raises and nothing below runs.
    begin
        insert into public.credit_ledger (user_id, delta, kind, reason, balance_after, created_at)
             values (p_user_id, p_amount, 'signup', 'Welcome to Carigma', 0, p_at);
    exception
        when unique_violation then
            -- Already granted. NOT an error: a second profile save is an
            -- ordinary thing to do, and it must be free of consequence.
            return null;
    end;

    insert into public.credits (user_id, balance, updated_at)
         values (p_user_id, p_amount, p_at)
    on conflict (user_id) do update
            set balance = public.credits.balance + p_amount,
                updated_at = p_at
      returning balance into v_balance;

    -- The ledger's `balance_after` was written before the balance existed.
    -- Correcting it here keeps the audit row true rather than approximately
    -- true, which is the whole reason a ledger is worth having.
    update public.credit_ledger
       set balance_after = v_balance
     where user_id = p_user_id
       and kind = 'signup';

    return v_balance;
end $$;

-- service_role ONLY. A function `authenticated` can execute is one anybody can
-- execute from a browser console, and this one mints credits. Revoked
-- explicitly rather than left to the default, which has changed before.
revoke all on function public.grant_signup_credits(uuid, integer, timestamptz) from public;
revoke all on function public.grant_signup_credits(uuid, integer, timestamptz) from anon;
revoke all on function public.grant_signup_credits(uuid, integer, timestamptz) from authenticated;
grant execute on function public.grant_signup_credits(uuid, integer, timestamptz) to service_role;

comment on function public.grant_signup_credits is
    'The welcome grant: ledger row and balance in one transaction, once per user, '
    'enforced by the partial unique index credit_ledger_one_signup_grant. '
    'service_role only — a version authenticated could call would mint credits from '
    'a browser console. Fired on profile COMPLETION, never on registration.';
