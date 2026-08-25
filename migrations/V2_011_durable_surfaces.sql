-- V2_011 — the three tables Posts, Tracker and the weekly review need.
--
-- ## Why this exists
--
-- `routes/posts.py` held every week plan in `_PLANS: dict[str, dict[str,
-- WeekPlan]]`. `routes/weekly.py` did the same with `_CONTRACTS` and
-- `_REVIEWS`. Module-level dictionaries. Both surfaces are marked Done, both
-- return well-formed payloads, and both persist NOTHING — one Render restart
-- loses every user's week plan, publish state and approved contract, and a
-- second instance gives two users different answers to the same question.
--
-- The schema snapshot proved `content_loop`, `tracker` and `weekly_review`
-- exist nowhere. They were named in an exemption list as "V1's tables", which
-- is how five imaginary tables passed a guard for a phase.
--
-- ## Shape
--
-- The column sets mirror the dataclasses that already model this — `WeekPlan`
-- / `Slot` in services/posts.py and `WeeklyContract` in services/weekly.py —
-- so the mapping is a translation rather than a redesign. Enum-ish columns
-- carry check constraints that MUST be kept in step with those StrEnums; the
-- service modules already carry that note for `weekly_contracts_state_check`.
--
-- Additive only. Nothing here touches a table V1 reads.

-- ── The content loop: one row per SLOT, not per week ───────────────────────
--
-- A slot is what the user acts on — drafts it, publishes it, skips it with a
-- reason. Storing a week as one JSON blob would make "mark Thursday published"
-- a read-modify-write of the whole week, which is how two tabs lose an edit.
create table if not exists public.content_loop (
    id            bigserial primary key,
    user_id       uuid not null references auth.users (id) on delete cascade,
    week_start    date not null,
    day           text not null,
    slot_date     date not null,
    time_label    text not null default '09:00',
    state         text not null default 'pending',
    optional      boolean not null default false,
    -- The drafts as generated, with their notes. Opaque to SQL on purpose:
    -- nothing queries inside a draft, and giving them their own table would
    -- buy a join for no reader.
    drafts        jsonb not null default '[]'::jsonb,
    published_at  timestamptz,
    -- Why a slot was skipped. V1's bug B was a reason that did not save; here
    -- it is a column, so "recorded" and "accepted and dropped" cannot look
    -- the same.
    skip_reason   text,
    skip_note     text,
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now(),
    -- One row per day per week per user. The upsert target, and the thing
    -- that stops a double-submit creating two Thursdays.
    unique (user_id, week_start, day)
);

alter table public.content_loop
    add constraint content_loop_state_check
    check (state in ('pending', 'drafted', 'published', 'skipped', 'missed'));

alter table public.content_loop enable row level security;

create policy content_loop_owner on public.content_loop
    for all to authenticated
    using (auth.uid() = user_id)
    with check (auth.uid() = user_id);

create index if not exists content_loop_user_week_idx
    on public.content_loop (user_id, week_start);

-- ── The week's cadence and streak, which belong to the WEEK not the slot ───
create table if not exists public.content_weeks (
    id               bigserial primary key,
    user_id          uuid not null references auth.users (id) on delete cascade,
    week_start       date not null,
    cadence_per_week integer not null default 2,
    cadence_days     jsonb not null default '["MON","THU"]'::jsonb,
    streak_weeks     integer not null default 0,
    -- A pause is a fact about what the user asked for, not a gap inferred from
    -- missing rows. Inferring it would make a holiday look like a lapse.
    paused_until     date,
    paused_because   text,
    created_at       timestamptz not null default now(),
    updated_at       timestamptz not null default now(),
    unique (user_id, week_start)
);

alter table public.content_weeks enable row level security;

create policy content_weeks_owner on public.content_weeks
    for all to authenticated
    using (auth.uid() = user_id)
    with check (auth.uid() = user_id);

-- ── The tracker: applications and their stages ────────────────────────────
create table if not exists public.tracker (
    id             bigserial primary key,
    user_id        uuid not null references auth.users (id) on delete cascade,
    -- Nullable: a user may track a role they found themselves, with no row in
    -- jobs_feed. No FK, because jobs_feed rows are pruned and losing the
    -- application when the listing ages out would be worse than a dangling id.
    job_id         bigint,
    title          text not null,
    company        text not null,
    stage          text not null default 'applied',
    applied_at     timestamptz,
    -- The one the Today ladder reads: an interview inside 72 hours outranks
    -- everything else competing for the primary action.
    interview_at   timestamptz,
    closed_at      timestamptz,
    closed_reason  text,
    notes          text,
    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now()
);

alter table public.tracker
    add constraint tracker_stage_check
    check (stage in ('applied', 'screening', 'interview', 'offer', 'closed'));

alter table public.tracker enable row level security;

create policy tracker_owner on public.tracker
    for all to authenticated
    using (auth.uid() = user_id)
    with check (auth.uid() = user_id);

-- Partial index: the ladder asks "any interview in the next 72 hours?" on
-- every Today load, and only open applications can answer yes.
create index if not exists tracker_upcoming_interview_idx
    on public.tracker (user_id, interview_at)
    where interview_at is not null and closed_at is null;

-- ── The weekly contract and its review ────────────────────────────────────
create table if not exists public.weekly_review (
    id              bigserial primary key,
    user_id         uuid not null references auth.users (id) on delete cascade,
    week_start      date not null,
    state           text not null default 'proposed',
    -- The proposed items, each with its minutes. Read as a whole, never
    -- filtered in SQL.
    items           jsonb not null default '[]'::jsonb,
    -- Partial approval is the design: a user may take the content plan and
    -- decline the scans. An empty array after a decision means they took
    -- nothing, which is different from never having decided — hence the
    -- separate `decided_at`.
    accepted_kinds  jsonb not null default '[]'::jsonb,
    cadence         text not null default 'weekly',
    proposed_at     timestamptz,
    decided_at      timestamptz,
    -- What the review actually reported, once the week closed.
    facts           jsonb,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now(),
    unique (user_id, week_start)
);

alter table public.weekly_review
    add constraint weekly_review_state_check
    check (state in ('proposed', 'approved', 'declined', 'lapsed', 'complete'));

alter table public.weekly_review enable row level security;

create policy weekly_review_owner on public.weekly_review
    for all to authenticated
    using (auth.uid() = user_id)
    with check (auth.uid() = user_id);
