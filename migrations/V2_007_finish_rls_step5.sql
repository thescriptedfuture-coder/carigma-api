-- ───────────────────────────────────────────────────────────────────────────
-- V2_007 — finish RLS step 5: the last nine tables
--
-- After V2_006, `payments` and `user_subscriptions` are owner-scoped. These
-- nine still carry `authenticated ... USING (true)`, so any signed-in user can
-- read and modify any other user's rows. This closes them.
--
-- They do NOT all want the same policy, and the difference is the point of
-- this file. Each table was placed by auditing who actually reaches it, not by
-- assuming that "has a user_id column" means "is user-facing".
--
-- ═══ GROUP A — RLS enabled, NO policies (service-role only) ════════════════
--
--   email_log    Written ONLY by the digest cron (scripts/send_digests.py:112,
--                137 -> db.log_email), which reroutes db.get_supabase to the
--                service-role client. Read ONLY by admin.py:177, behind
--                is_admin(). No user-facing path exists.
--
--                A per-user policy would also be actively WRONG here:
--                `email_log.user_id` is NULLABLE ("some sends aren't
--                user-scoped"). Under auth.uid() = user_id a NULL row is
--                invisible to everyone and un-insertable, so the audit log
--                would silently lose exactly the rows nobody owns.
--
--   digest_log   Written ONLY by db.record_digest_sent, called ONLY from the
--                cron (send_digests.py:105, 116). It is the duplicate-send
--                guard — a unique conflict means "already sent". Nothing
--                user-facing reads or writes it.
--
--   jobs_cache   No `user_id` column AT ALL. Keyed on the query (role×city),
--                shared across every user, and its row count for today IS the
--                global JSearch daily-cap counter. Reached only through the
--                service role as of V1 commit 17b3eb2.
--
--                Giving this table auth.uid() = user_id would reference a
--                column that does not exist and lock it to nobody — which,
--                because the cap counter fails closed, would stop job scanning
--                entirely. It must never be swept into a per-user batch.
--
-- ═══ GROUP B — auth.uid() = user_id ════════════════════════════════════════
--
--   Ordered by sensitivity, most sensitive first, per the review.
--
--   content_plan        unpublished drafts — someone's unsent words
--   interview_preps     prep kits tied to named companies
--   application_events  who applied where, and when
--   content_feedback    what they told us privately about a draft
--   jobs_feed           which roles they were shown and dismissed
--   milestones          progress markers
--
--   All six have `user_id uuid NOT NULL references auth.users`, and all six are
--   reached from user sessions on the main thread. Reads that filter by
--   `application_id` or `id` rather than `user_id` (db.py:918, 989, and the
--   jobs_feed updates) stay correct: RLS ANDs the owner check onto the query,
--   which is precisely the backstop those call sites were missing.
--
-- ## Preconditions
--
-- Asserted per table before anything changes: the table exists, RLS is
-- genuinely ENABLED, and — for group B only — a `user_id` column exists. The
-- RLS check is this thread's hardest-won lesson: a policy on a table with RLS
-- off is stored, never enforced, and looks correct in the dashboard.
--
-- ## Idempotent
--
-- `drop policy if exists` + create, the idiom tests/test_migrations.py
-- sanctions. Re-running produces the same end state.
-- ───────────────────────────────────────────────────────────────────────────

do $$
declare
    t text;
    p record;
    service_only text[] := array['email_log', 'digest_log', 'jobs_cache'];
    owner_scoped text[] := array[
        'content_plan',
        'interview_preps',
        'application_events',
        'content_feedback',
        'jobs_feed',
        'milestones'
    ];
begin
    -- ── Shared preconditions ───────────────────────────────────────────────
    foreach t in array (service_only || owner_scoped) loop
        if not exists (
            select 1 from pg_class c
              join pg_namespace n on n.oid = c.relnamespace
             where n.nspname = 'public' and c.relname = t
        ) then
            raise exception 'V2_007: table public.% does not exist', t;
        end if;

        if not exists (
            select 1 from pg_class c
              join pg_namespace n on n.oid = c.relnamespace
             where n.nspname = 'public' and c.relname = t and c.relrowsecurity
        ) then
            raise exception 'V2_007: RLS is not enabled on public.% — a policy '
                            'would be stored but never enforced', t;
        end if;
    end loop;

    -- Group B additionally needs the column its policy names.
    foreach t in array owner_scoped loop
        if not exists (
            select 1 from information_schema.columns
             where table_schema = 'public' and table_name = t
               and column_name = 'user_id'
        ) then
            raise exception 'V2_007: public.% has no user_id column', t;
        end if;

        -- A NULLABLE user_id means some rows are owned by nobody, and a
        -- per-user policy would hide them permanently. email_log is exactly
        -- that case, which is why it is in group A instead.
        if exists (
            select 1 from information_schema.columns
             where table_schema = 'public' and table_name = t
               and column_name = 'user_id' and is_nullable = 'YES'
        ) then
            raise exception 'V2_007: public.%.user_id is nullable — an owner '
                            'policy would orphan its NULL rows. Move it to the '
                            'service-only group instead.', t;
        end if;
    end loop;

    -- ── Group A: remove the permissive policy, replace with nothing ────────
    -- RLS with no policies denies anon and authenticated everything. The
    -- service role bypasses RLS, so every real caller is unaffected.
    foreach t in array service_only loop
        for p in
            select policyname from pg_policies
             where schemaname = 'public' and tablename = t and qual = 'true'
        loop
            execute format('drop policy if exists %I on public.%I', p.policyname, t);
            raise notice 'V2_007: % on % -> removed (service-role only)', p.policyname, t;
        end loop;
    end loop;

    -- ── Group B: scope to the row's owner ──────────────────────────────────
    foreach t in array owner_scoped loop
        for p in
            select policyname from pg_policies
             where schemaname = 'public' and tablename = t and qual = 'true'
        loop
            execute format('drop policy if exists %I on public.%I', p.policyname, t);
            execute format(
                'create policy %I on public.%I as permissive for all to authenticated '
                'using (auth.uid() = user_id) with check (auth.uid() = user_id)',
                p.policyname, t
            );
            raise notice 'V2_007: % on % -> auth.uid() = user_id', p.policyname, t;
        end loop;
    end loop;
end $$;


-- ───────────────────────────────────────────────────────────────────────────
-- Verification 1 — no permissive-to-everyone policy survives anywhere.
--
-- Expect ZERO rows.
-- ───────────────────────────────────────────────────────────────────────────
select tablename, policyname, cmd, roles, qual
  from pg_policies
 where schemaname = 'public'
   and qual = 'true'
   and tablename in (
       'application_events', 'content_feedback', 'content_plan', 'digest_log',
       'email_log', 'interview_preps', 'jobs_cache', 'jobs_feed', 'milestones',
       'payments', 'user_subscriptions'
   );


-- ───────────────────────────────────────────────────────────────────────────
-- Verification 2 — every table landed in the group it was meant to.
--
-- Expect:
--   email_log / digest_log / jobs_cache            rls_enabled=t, policies=0
--   the six in group B                             rls_enabled=t, policies=1
--   payments / user_subscriptions (from V2_006)    rls_enabled=t, policies=1
--
-- A group B table showing 0 is locked to nobody; a group A table showing 1 did
-- not get cleared. Either is a failure, and neither raises an error by itself.
-- ───────────────────────────────────────────────────────────────────────────
select
    c.relname                        as table_name,
    c.relrowsecurity                 as rls_enabled,
    (select count(*)
       from pg_policies p
      where p.schemaname = 'public'
        and p.tablename = c.relname) as policy_count
  from pg_class c
  join pg_namespace n on n.oid = c.relnamespace
 where n.nspname = 'public'
   and c.relname in (
       'application_events', 'content_feedback', 'content_plan', 'digest_log',
       'email_log', 'interview_preps', 'jobs_cache', 'jobs_feed', 'milestones',
       'payments', 'user_subscriptions'
   )
 order by c.relname;

-- Untouched, each for a reason already established:
--   feedback         anon INSERT is intentional — write-only by design
--   market_digests   authenticated + published only
--   admin_notes      RLS on, no policies — service-role only
--   credit_requests  same
--
-- Then, from carigma-api:  python scripts/verify_rls.py
-- Anonymous access is already closed, so this is a regression check rather
-- than the proof of this migration. The proof is the two queries above, plus
-- signed-in V1: drafts, prep kits, the jobs feed and the tracker timeline all
-- still load, and the digest cron still records its sends.
