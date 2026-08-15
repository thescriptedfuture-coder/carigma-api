-- V2_012 — make the check constraints say what the code can actually write.
--
-- Two problems, one shape.
--
-- 1. `weekly_contracts_state_check` (V2_001) permits 'expired' and 'lapsed'.
--    Nothing has ever written them — `count_lapses` counted three states of
--    which two were dead, and `auto_adopt()` had no caller — so the escalation
--    they served could never fire. The enum has dropped them; the constraint
--    should not keep advertising values the application cannot produce.
--
-- 2. `weekly_review_state_check` (V2_011, mine, three commits ago) permits
--    'lapsed' and 'complete' and **omits 'auto_adopted'** — the one state the
--    Sunday sweep is about to start writing. It would have rejected every
--    auto-adopted week with a constraint violation.
--
-- The second is the more instructive: I wrote the constraint from the shape of
-- the surface rather than from the enum, and nothing compared them. So this
-- migration ships alongside a test that walks `ContractState` and the SQL and
-- fails when they disagree — the docstring said "keep them in step" and that
-- was the whole mechanism.
--
-- 3. `content_loop_state_check` (V2_011, mine, same file) permits 'drafted'
--    and 'missed'. `SlotState` has 'draft_ready' and no 'missed' at all. So a
--    slot could never reach the drafted state without violating a constraint.
--
--    Two wrong constraints in one migration, both written from the shape of
--    the surface rather than from the enum, is the argument for the test
--    rather than for being more careful next time.
--
-- All three constraints are V2-owned, so dropping them is not a change to anything
-- V1 relies on.
-- carigma:v2-owned-constraint:weekly_contracts_state_check
-- carigma:v2-owned-constraint:weekly_review_state_check
-- carigma:v2-owned-constraint:content_loop_state_check

alter table public.weekly_contracts
    drop constraint if exists weekly_contracts_state_check;

alter table public.weekly_contracts
    add constraint weekly_contracts_state_check
    check (state in ('proposed', 'approved', 'auto_adopted', 'declined'));

alter table public.weekly_review
    drop constraint if exists weekly_review_state_check;

alter table public.weekly_review
    add constraint weekly_review_state_check
    check (state in ('proposed', 'approved', 'auto_adopted', 'declined'));

alter table public.content_loop
    drop constraint if exists content_loop_state_check;

alter table public.content_loop
    add constraint content_loop_state_check
    check (state in ('pending', 'draft_ready', 'published', 'skipped'));
