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

-- ## Why no `DO $$ … $$` block
--
-- The first version wrapped the check in PL/pgSQL. The Supabase SQL editor
-- answered `42601: syntax error at or near "if"` — it does not carry the
-- dollar-quoted body through to the server intact, so the `begin … end` was
-- parsed as plain SQL. The file was valid PostgreSQL and still unrunnable in
-- the one place it has to run.
--
-- Plain statements instead. Step 1 REFUSES by failing: casting the message to
-- `int` raises, and the message is the error text, so the reason arrives with
-- the failure rather than in a comment nobody reads.

-- ── 1 · Refuse if anything has started writing to it ───────────────────────
-- Expect: one row, `safe_to_drop = 0`.
-- If it raises `invalid input syntax for type integer: "tracker is not
-- empty …"`, STOP. Something writes to this table now, and that is a
-- conversation rather than a drop.
--
-- The passing branch is `'0'` rather than `'ok'` because BOTH branches are
-- cast, and `'ok'::int` raises exactly like the refusal does — which would
-- have made the safe path fail and the failure mean nothing.
select (
  case
    when (select count(*) from public.tracker) = 0 then '0'
    else 'tracker is not empty - something writes to it now - do not drop it'
  end
)::int as safe_to_drop;

-- ── 2 · The drop ──────────────────────────────────────────────────────────
-- `if exists` so re-running is a no-op. NO `cascade`: if anything has grown a
-- dependency since, this must FAIL and be looked at rather than quietly take
-- the dependant with it.
drop table if exists public.tracker;
