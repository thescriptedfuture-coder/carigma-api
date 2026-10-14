-- ═══════════════════════════════════════════════════════════════════════════
-- V2_017 · Drop `tracker`, the table nothing ever wrote to
--
-- ## What it was
--
-- A V2-era applications table with plausible columns — `stage`,
-- `interview_at`, `closed_at` — and **zero rows**, for its entire life.
--
-- V1's applications live in `job_applications`: 13 rows, UUID `id`, columns
-- `company / role / status / link / salary / notes / interview_on /
-- closed_reason`. Both `application_events.application_id` and
-- `interview_preps.application_id` are UUIDs matching `job_applications.id`
-- and nothing in `tracker`, so the parallel table was never wired to anything
-- either.
--
-- ## Why it has to go rather than sit there
--
-- `services/today.py` READ it, from the day the ladder was built. The read
-- SUCCEEDED and returned nothing, so Today's interview rung could never fire
-- and the pipeline stat reported a confident "0 active" to the one user with
-- thirteen applications. Nothing errored; nothing could.
--
-- That is the orphan class with a schema attached, and leaving an empty table
-- with the right-looking columns beside the real one is how the next reader
-- picks the wrong one — exactly as this one did. A field nothing renders is a
-- trap; a table nothing writes is a bigger one.
--
-- ## Safety
--
-- Verified empty immediately before writing this, as the service role:
--
--     select count(*) from tracker;   -- 0
--
-- `IF EXISTS` so re-running is a no-op, and NO `CASCADE`: if anything has
-- grown a dependency on this table since, the drop must FAIL and be looked at
-- rather than quietly take the dependant with it.
--
-- ## The ownership claim, and its proof
--
-- carigma:v2-owned-table: tracker
--
-- `tests/test_migrations.py` verifies that marker rather than trusting it: the
-- name must appear in an earlier V2 migration's `create table`, and it does —
-- V2_011 created it. V1 never reads or writes it; its applications are in
-- `job_applications`, and the only "tracker" in V1's source is a nav label.
--
-- Idempotent. Safe to run twice.
-- ═══════════════════════════════════════════════════════════════════════════

do $$
begin
  -- Refuse rather than destroy. A row here means something started writing
  -- after this migration was written, and that is a conversation, not a drop.
  if exists (select 1 from information_schema.tables
             where table_schema = 'public' and table_name = 'tracker')
     and (select count(*) from public.tracker) > 0 then
    raise exception
      'tracker is not empty — something writes to it now. Do not drop it.';
  end if;
end $$;

drop table if exists public.tracker;
