-- ───────────────────────────────────────────────────────────────────────────
-- V2_014 — make scaling past one instance impossible to do by ACCIDENT
--
-- `services/ratelimit` holds its token buckets in process memory. On one
-- instance that is correct and cheap. On two, **every ceiling silently
-- doubles** — no error, no log line, just twice the intended rate against a
-- JSearch quota that live V1 users are also drawing from.
--
-- ## Why not Redis
--
-- Because we do not have this problem. One instance is right at this scale,
-- and adding a Redis to solve a hypothetical is how a solo founder acquires an
-- operational burden before acquiring users. The decision is to KEEP the
-- in-process limiter and make its constraint visible.
--
-- ## Why a declared setting is not enough
--
-- `MAX_INSTANCES=1` in the environment catches an operator who edits env vars.
-- It does not catch the actual accident: someone raising the instance count in
-- Render's dashboard, which touches no environment variable at all. The
-- declaration would still say 1 and nothing would fire.
--
-- So the process OBSERVES its peers. Each one upserts a heartbeat on startup
-- and periodically after; counting distinct recent rows answers "how many of
-- us are there" from inside a process that cannot otherwise know.
--
-- ## Why it warns rather than refuses
--
-- A rolling deploy briefly runs old and new together. Refusing to boot would
-- turn every routine deploy into an outage, which is a worse failure than the
-- one being prevented. Instead it logs CRITICAL and raises an admin-visible
-- flag — the alarm fires, the deploy completes, and the overlap clears itself.
--
-- The rule this follows: **produce a question rather than a bill.** Same shape
-- as `JSEARCH_DAILY_CAP` sitting far below the plan limit.
--
-- Service-role only. An instance list is infrastructure, not user data.
-- carigma:rls-service-role-only
-- ───────────────────────────────────────────────────────────────────────────

create table if not exists public.api_instances (
    --: Generated per PROCESS, not per deploy. Two processes from one image are
    --: two instances, which is exactly the thing being counted.
    instance_id text primary key,
    first_seen  timestamptz not null default now(),
    last_seen   timestamptz not null default now(),
    --: Carried so a stale row can be attributed to a specific release rather
    --: than guessed at.
    version     text
);

create index if not exists api_instances_last_seen_idx
    on public.api_instances (last_seen desc);

alter table public.api_instances enable row level security;


-- ───────────────────────────────────────────────────────────────────────────
-- Verification
--
-- Expect: api_instances rls=true policies=0
-- ───────────────────────────────────────────────────────────────────────────
select
    c.relname as table_name,
    c.relrowsecurity as rls,
    (select count(*) from pg_policies p
      where p.schemaname = 'public' and p.tablename = c.relname) as policy_count
from pg_class c
join pg_namespace n on n.oid = c.relnamespace
where n.nspname = 'public' and c.relname = 'api_instances';
