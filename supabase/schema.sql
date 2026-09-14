-- Job Scout — Supabase schema
--
-- Apply once in the Supabase SQL editor, or with `supabase db push`.
-- Safe to re-run: every statement is idempotent.
--
-- Six tables, matching ARCHITECTURE.md section 5.3. `job_results` is the one
-- the frontend reads; the rest support it.
--
-- Security model: every table carries `user_id references auth.users`, and RLS
-- restricts every row to its owner. The backend connects with the service-role
-- key and therefore BYPASSES RLS - it filters by user_id in Python instead
-- (see jobscout/store/supabase_store.py). RLS is what protects the frontend,
-- which connects with the anon key plus the signed-in user's JWT.

-- ---------------------------------------------------------------------------
-- 0. Helpers
-- ---------------------------------------------------------------------------

create extension if not exists "pgcrypto";   -- gen_random_uuid()

create or replace function public.touch_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;


-- ---------------------------------------------------------------------------
-- 1. profiles — one job search configuration per user
-- ---------------------------------------------------------------------------
-- `config` is UserProfile.model_dump() minus the CV. Kept as jsonb because a
-- profile is read and written whole and never queried by its parts.

create table if not exists public.profiles (
    id            uuid primary key default gen_random_uuid(),
    user_id       uuid not null references auth.users (id) on delete cascade,
    profile_id    text not null,                 -- "gabriel", "camila", ...
    display_name  text not null default '',
    config        jsonb not null default '{}'::jsonb,
    cv_text       text not null default '',      -- personal data: RLS matters here
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now(),

    constraint profiles_user_profile_unique unique (user_id, profile_id)
);

create index if not exists profiles_user_idx on public.profiles (user_id);

drop trigger if exists profiles_touch_updated_at on public.profiles;
create trigger profiles_touch_updated_at
    before update on public.profiles
    for each row execute function public.touch_updated_at();


-- ---------------------------------------------------------------------------
-- 2. runs — one row per execution
-- ---------------------------------------------------------------------------
-- `cost_fingerprint` is stored so the second confirmation of a held run can
-- arrive in a separate HTTP request: the server does not keep the estimate in
-- memory between the two.

create table if not exists public.runs (
    run_id            text primary key,
    user_id           uuid not null references auth.users (id) on delete cascade,
    profile_id        text not null default '',
    started_at        timestamptz,
    finished_at       timestamptz,
    status            text not null default '',
    cost_decision     text not null default '',
    cost_estimate     jsonb not null default '{}'::jsonb,
    cost_fingerprint  text not null default '',
    counts            jsonb not null default '{}'::jsonb,
    created_at        timestamptz not null default now(),

    constraint runs_status_valid check (
        status in ('', 'OK', 'NEEDS_CONFIRMATION', 'REJECTED_OVER_HARD_CAP')
    )
);

create index if not exists runs_user_started_idx
    on public.runs (user_id, started_at desc);
create index if not exists runs_user_fingerprint_idx
    on public.runs (user_id, cost_fingerprint);


-- ---------------------------------------------------------------------------
-- 3. seen_jobs — the deduplication cache
-- ---------------------------------------------------------------------------
-- Replaces seen_jobs.json. Per user by construction, which the single shared
-- file could never be.

create table if not exists public.seen_jobs (
    user_id       uuid not null references auth.users (id) on delete cascade,
    job_key       text not null,
    first_seen_at timestamptz not null default now(),

    primary key (user_id, job_key)
);


-- ---------------------------------------------------------------------------
-- 4. job_results — THE DELIVERABLE
-- ---------------------------------------------------------------------------
-- This is what the frontend queries to show a user their offers.
--
-- `unique (user_id, url)` is load-bearing: it is what stops the same posting
-- being scored (and billed) twice across runs. It replaces the old
-- load_tracker_urls(), which re-read an .xlsx file to achieve the same thing.

create table if not exists public.job_results (
    id                   uuid primary key default gen_random_uuid(),
    user_id              uuid not null references auth.users (id) on delete cascade,
    run_id               text not null default '',
    job_key              text not null default '',

    title                text not null default '',
    company              text not null default '',
    url                  text not null,
    source               text not null default '',
    published            text not null default '',
    location             text not null default '',

    verdict              text not null default 'NO',
    score                numeric(4, 1) not null default 0,
    base_score           numeric(4, 1) not null default 0,
    breakdown            jsonb not null default '{}'::jsonb,   -- {dimension: 0-10}

    one_liner            text not null default '',
    match_signals        text[] not null default '{}',
    gaps                 text[] not null default '{}',
    red_flags            text[] not null default '{}',
    flags                jsonb not null default '{}'::jsonb,   -- {llm_flag: bool}
    extra                jsonb not null default '{}'::jsonb,   -- profile-declared fields

    generated_text       text not null default '',
    salary_range_market  text not null default '',
    evaluated_at         timestamptz,
    created_at           timestamptz not null default now(),

    constraint job_results_user_url_unique unique (user_id, url),
    constraint job_results_verdict_valid check (verdict in ('YES', 'MAYBE', 'NO')),
    constraint job_results_score_range check (score >= 0 and score <= 10),
    constraint job_results_base_score_range check (base_score >= 0 and base_score <= 10)
);

-- The frontend's main query: this user's offers, best first.
create index if not exists job_results_user_score_idx
    on public.job_results (user_id, score desc);
-- Filtering by verdict in the UI.
create index if not exists job_results_user_verdict_idx
    on public.job_results (user_id, verdict);
-- Looking up what one run produced.
create index if not exists job_results_run_idx
    on public.job_results (user_id, run_id);


-- ---------------------------------------------------------------------------
-- 5. raw_dumps — paid connector payloads, replayable at zero cost
-- ---------------------------------------------------------------------------
-- The payload itself goes to Supabase Storage (a dump is ~1.5-32 MB, which is
-- unpleasant in jsonb); only the pointer lives here.

create table if not exists public.raw_dumps (
    id            uuid primary key default gen_random_uuid(),
    user_id       uuid not null references auth.users (id) on delete cascade,
    run_id        text not null default '',
    connector     text not null,
    actor_id      text not null default '',
    apify_run_id  text not null default '',
    items_count   integer not null default 0,
    storage_path  text not null default '',
    saved_at      timestamptz not null default now(),

    constraint raw_dumps_user_connector_unique unique (user_id, connector, run_id)
);

create index if not exists raw_dumps_user_connector_idx
    on public.raw_dumps (user_id, connector, saved_at desc);


-- ---------------------------------------------------------------------------
-- 6. applications — the user's own tracking
-- ---------------------------------------------------------------------------
-- Separate from job_results on purpose. In the notebooks the Tracker sheet was
-- deleted and rebuilt on every run, so a status edited by hand was silently
-- overwritten. Here a run writes job_results and never touches this table.

create table if not exists public.applications (
    id             uuid primary key default gen_random_uuid(),
    user_id        uuid not null references auth.users (id) on delete cascade,
    job_result_id  uuid not null references public.job_results (id) on delete cascade,
    status         text not null default '',
    priority       text not null default '',
    target_salary  text not null default '',
    notes          text not null default '',
    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now(),

    constraint applications_user_job_unique unique (user_id, job_result_id)
);

create index if not exists applications_user_idx on public.applications (user_id);

drop trigger if exists applications_touch_updated_at on public.applications;
create trigger applications_touch_updated_at
    before update on public.applications
    for each row execute function public.touch_updated_at();


-- ---------------------------------------------------------------------------
-- 7. Row Level Security
-- ---------------------------------------------------------------------------
-- The security boundary of the whole multi-user design. Without these, any
-- signed-in user could read every other user's CV and results.
--
-- One policy per table, covering all four commands, with both USING (which
-- rows are visible) and WITH CHECK (which rows may be written). Splitting them
-- is what prevents a user inserting a row owned by someone else.

alter table public.profiles     enable row level security;
alter table public.runs         enable row level security;
alter table public.seen_jobs    enable row level security;
alter table public.job_results  enable row level security;
alter table public.raw_dumps    enable row level security;
alter table public.applications enable row level security;

-- Also apply RLS to the table owner, so a mistake cannot be masked by
-- privileged access during development.
alter table public.profiles     force row level security;
alter table public.runs         force row level security;
alter table public.seen_jobs    force row level security;
alter table public.job_results  force row level security;
alter table public.raw_dumps    force row level security;
alter table public.applications force row level security;

do $$
declare
    t text;
begin
    foreach t in array array[
        'profiles', 'runs', 'seen_jobs', 'job_results', 'raw_dumps', 'applications'
    ]
    loop
        execute format('drop policy if exists %I on public.%I', t || '_owner_rw', t);
        execute format($f$
            create policy %I on public.%I
                for all
                to authenticated
                using (auth.uid() = user_id)
                with check (auth.uid() = user_id)
        $f$, t || '_owner_rw', t);
    end loop;
end
$$;

-- Nothing is granted to `anon`: an unauthenticated visitor sees no row at all.
revoke all on public.profiles, public.runs, public.seen_jobs,
                public.job_results, public.raw_dumps, public.applications
    from anon;

grant select, insert, update, delete
    on public.profiles, public.runs, public.seen_jobs,
       public.job_results, public.raw_dumps, public.applications
    to authenticated;


-- ---------------------------------------------------------------------------
-- 8. Verification
-- ---------------------------------------------------------------------------
-- Run this after applying the file. Every table must show rls_enabled = true
-- and exactly one policy.

-- select
--     c.relname                as table_name,
--     c.relrowsecurity         as rls_enabled,
--     c.relforcerowsecurity    as rls_forced,
--     count(p.polname)         as policies
-- from pg_class c
-- join pg_namespace n on n.oid = c.relnamespace
-- left join pg_policy p on p.polrelid = c.oid
-- where n.nspname = 'public'
--   and c.relname in ('profiles','runs','seen_jobs','job_results',
--                     'raw_dumps','applications')
-- group by 1, 2, 3
-- order by 1;
