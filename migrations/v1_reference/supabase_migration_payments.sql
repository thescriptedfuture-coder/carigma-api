-- ════════════════════════════════════════════════════════════════════════
-- Carigma — payments (Razorpay top-ups + subscriptions)
-- payments:            one row per top-up Payment Link (created → paid)
-- user_subscriptions:  one row per Razorpay subscription (mandate);
--                      cycles_credited tracks how many monthly charges have
--                      already been converted into credits (idempotent).
-- Run ONCE:  Supabase → SQL Editor → paste CONTENTS → Run.  Safe to re-run.
-- ════════════════════════════════════════════════════════════════════════

create table if not exists public.payments (
    id          bigint generated always as identity primary key,
    user_id     uuid not null references auth.users (id) on delete cascade,
    provider    text not null default 'razorpay',
    link_id     text unique,                  -- Razorpay payment-link id
    payment_id  text,                         -- Razorpay payment id (once paid)
    pack_key    text,                         -- CREDIT_PACKS key
    credits     integer not null,
    amount_inr  integer not null,
    status      text not null default 'created',   -- created | paid | expired
    short_url   text,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);
create index if not exists payments_user_idx
    on public.payments (user_id, created_at desc);

create table if not exists public.user_subscriptions (
    id              bigint generated always as identity primary key,
    user_id         uuid not null references auth.users (id) on delete cascade,
    provider        text not null default 'razorpay',
    sub_id          text unique not null,     -- Razorpay subscription id
    plan_key        text not null,            -- SUBSCRIPTION_PLANS key
    status          text not null default 'created',
    cycles_credited integer not null default 0,
    short_url       text,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now()
);
create index if not exists user_subscriptions_user_idx
    on public.user_subscriptions (user_id, created_at desc);

-- RLS: the server-side app (anon key) reads/writes these; rows are keyed and
-- filtered by user_id in the app. Same app-server pattern as the feedback fix.
alter table public.payments           enable row level security;
alter table public.user_subscriptions enable row level security;

drop policy if exists "payments app access" on public.payments;
create policy "payments app access" on public.payments
    for all to anon, authenticated using (true) with check (true);

drop policy if exists "user_subscriptions app access" on public.user_subscriptions;
create policy "user_subscriptions app access" on public.user_subscriptions
    for all to anon, authenticated using (true) with check (true);
