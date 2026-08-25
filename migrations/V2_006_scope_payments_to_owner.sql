-- ───────────────────────────────────────────────────────────────────────────
-- V2_006 — tighten `payments` and `user_subscriptions` to the row's owner
--
-- Step 5 of the RLS plan, taking the two tables where a wrong id has FINANCIAL
-- consequences first. V2_005 removed `anon` from the eleven permissive V1
-- tables but left `authenticated` with USING (true) — so any signed-in user can
-- currently read and modify any other user's payment and subscription rows.
--
-- ## Why these two, and why now
--
-- They were the only unbackstopped writes left after the application-side fix.
-- `payments.py` now scopes all four of its writes with `.eq("user_id", uid)`
-- (V1 commit 0dd89e4), but that is defence in depth, not a backstop: it holds
-- only as long as every future call site remembers. This makes the database
-- enforce it.
--
-- Unblocked by the V1 remediation deployed 2026-08-11: per-session client
-- isolation plus the SUPABASE_KEY swap to the publishable key. Before that,
-- V1 ran as service-role (RLS bypassed, so this would have changed nothing)
-- and the shared client carried an arbitrary user's identity (so this would
-- have served the wrong rows). Both are fixed and verified in production.
--
-- ## Safe for every writer
--
--   V1 insert   payments.py:109 / 172 — carries user_id in the row body, on the
--               main thread with the caller's session. Needs WITH CHECK.
--   V1 update   payments.py:207 / 257 / 284 — all now .eq("user_id", uid).
--   V1 select   reconcile() — already .eq("user_id", uid).
--   V2 API      routes/payments.py:148 uses `user_client`, so it acts AS the
--               caller. No service-role path and no unauthenticated webhook:
--               V1 reconciles on page load by design (Streamlit cannot expose
--               an HTTP route), and V2 verifies in-request.
--
-- Both tables have `user_id uuid not null references auth.users`, so
-- auth.uid() = user_id is well defined for every existing row.
--
-- ## NOT in this migration
--
-- The other nine of the eleven. `jobs_cache` in particular must NEVER get this
-- policy — it has no user_id column at all. It is shared infrastructure keyed
-- on the query, its row count is the global JSearch cap counter, and as of V1
-- commit 17b3eb2 it is reached only through the service role. Its end state is
-- RLS with NO policies, like admin_notes and credit_requests — not a per-user
-- rule it cannot satisfy.
--
-- ## Idempotent
--
-- `drop policy if exists` + `create policy`, the idiom tests/test_migrations.py
-- sanctions. Re-running produces the same end state.
-- ───────────────────────────────────────────────────────────────────────────

do $$
declare
    t text;
    p record;
    tables text[] := array['payments', 'user_subscriptions'];
begin
    foreach t in array tables loop
        -- Preconditions, asserted before anything is changed. A missing table
        -- or column means this is running against the wrong database, and a
        -- policy referencing a column that does not exist would lock the table
        -- to nobody.
        if not exists (
            select 1 from pg_class c
              join pg_namespace n on n.oid = c.relnamespace
             where n.nspname = 'public' and c.relname = t
        ) then
            raise exception 'V2_006: table public.% does not exist', t;
        end if;

        if not exists (
            select 1 from information_schema.columns
             where table_schema = 'public' and table_name = t
               and column_name = 'user_id'
        ) then
            raise exception 'V2_006: public.% has no user_id column', t;
        end if;

        if not exists (
            select 1 from pg_class c
              join pg_namespace n on n.oid = c.relnamespace
             where n.nspname = 'public' and c.relname = t and c.relrowsecurity
        ) then
            raise exception 'V2_006: RLS is not enabled on public.% — a policy '
                            'would be stored but never enforced', t;
        end if;

        -- Rebuild every policy that currently admits everyone. Matching on
        -- `qual = 'true'` rather than on a name, because the names came from
        -- V1 and are not guaranteed.
        for p in
            select policyname, cmd
              from pg_policies
             where schemaname = 'public'
               and tablename = t
               and qual = 'true'
        loop
            execute format('drop policy if exists %I on public.%I', p.policyname, t);
            execute format(
                'create policy %I on public.%I as permissive for all to authenticated '
                'using (auth.uid() = user_id) with check (auth.uid() = user_id)',
                p.policyname, t
            );
            raise notice 'V2_006: % on % -> auth.uid() = user_id', p.policyname, t;
        end loop;
    end loop;
end $$;


-- ───────────────────────────────────────────────────────────────────────────
-- Verification — run this after, and read the result.
--
-- Expect exactly one row per table, with BOTH qual and with_check reading
-- (auth.uid() = user_id). A null with_check would let a user write a row
-- owned by someone else while only being able to read their own.
-- ───────────────────────────────────────────────────────────────────────────
select tablename, policyname, cmd, roles, qual, with_check
  from pg_policies
 where schemaname = 'public'
   and tablename in ('payments', 'user_subscriptions')
 order by tablename, policyname;

-- Any row still showing qual = true is a policy this migration did not match.
--
-- Then, from carigma-api:  python scripts/verify_rls.py
-- Anonymous access is already closed, so the probe should be unchanged — it is
-- run as a regression check, not as the proof of this migration. The proof of
-- THIS one is the query above plus a signed-in check that a user still sees
-- their own payment history in V1's Pricing page.
