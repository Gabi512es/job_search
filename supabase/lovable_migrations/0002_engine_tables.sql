-- Engine tables: runs, job_results, seen_jobs, raw_dumps, applications.
--
-- public.profiles already exists (migration 0000) and is NOT touched here.
-- These tables reference auth.users(id) directly, so they do not depend on a
-- profiles row existing.
--
-- Written to match the conventions of 0000 and 0001: uppercase keywords, one
-- named policy per command, grants to both authenticated and service_role.
-- The engine's backend connects with the service-role key, so without those
-- grants it could not write anything.
--
-- One deliberate deviation: IF NOT EXISTS / DROP POLICY IF EXISTS. Drizzle
-- runs a migration once, so guards are normally redundant. They are here
-- because the same statements may already have been applied by hand to this
-- project, and a bare CREATE would abort the whole migration.

-- ---------------------------------------------------------------------------
-- 1. runs — one row per execution of the engine
-- ---------------------------------------------------------------------------
-- cost_fingerprint is stored so that the confirmation of a held run can arrive
-- in a separate HTTP request: the server keeps no estimate in memory between
-- the two. RUNNING and FAILED are written by the HTTP API around its
-- background task.

CREATE TABLE IF NOT EXISTS public.runs (
  run_id            TEXT PRIMARY KEY,
  user_id           UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  profile_id        TEXT NOT NULL DEFAULT '',
  started_at        TIMESTAMPTZ,
  finished_at       TIMESTAMPTZ,
  status            TEXT NOT NULL DEFAULT '',
  cost_decision     TEXT NOT NULL DEFAULT '',
  cost_estimate     JSONB NOT NULL DEFAULT '{}'::jsonb,
  cost_fingerprint  TEXT NOT NULL DEFAULT '',
  counts            JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT runs_status_valid CHECK (
    status IN ('', 'RUNNING', 'OK', 'NEEDS_CONFIRMATION',
               'AWAITING_SELECTION', 'REJECTED_OVER_HARD_CAP', 'FAILED')
  )
);

CREATE INDEX IF NOT EXISTS runs_user_started_idx
  ON public.runs (user_id, started_at DESC);
CREATE INDEX IF NOT EXISTS runs_user_fingerprint_idx
  ON public.runs (user_id, cost_fingerprint);

-- ---------------------------------------------------------------------------
-- 2. job_results — the deliverable the frontend renders
-- ---------------------------------------------------------------------------
-- UNIQUE (user_id, url) is load-bearing: it is what stops the same posting
-- being scored, and billed, twice across runs.
--
-- score is the final value after every bonus and penalty; base_score is the
-- weighted average before them. When the two differ, an adjustment fired and
-- the reason is in red_flags or match_signals, prefixed [-N.N] or [+N.N].
--
-- breakdown keys differ per user (tech_fit/location/company_size/seniority vs
-- perfil_fit/location/entidad_fit/condiciones), so the UI must read them from
-- the row rather than hardcode them.

CREATE TABLE IF NOT EXISTS public.job_results (
  id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id              UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  run_id               TEXT NOT NULL DEFAULT '',
  job_key              TEXT NOT NULL DEFAULT '',

  title                TEXT NOT NULL DEFAULT '',
  company              TEXT NOT NULL DEFAULT '',
  url                  TEXT NOT NULL,
  source               TEXT NOT NULL DEFAULT '',
  published            TEXT NOT NULL DEFAULT '',
  location             TEXT NOT NULL DEFAULT '',

  verdict              TEXT NOT NULL DEFAULT 'NO',
  score                NUMERIC(4,1) NOT NULL DEFAULT 0,
  base_score           NUMERIC(4,1) NOT NULL DEFAULT 0,
  breakdown            JSONB NOT NULL DEFAULT '{}'::jsonb,

  one_liner            TEXT NOT NULL DEFAULT '',
  match_signals        TEXT[] NOT NULL DEFAULT '{}',
  gaps                 TEXT[] NOT NULL DEFAULT '{}',
  red_flags            TEXT[] NOT NULL DEFAULT '{}',
  flags                JSONB NOT NULL DEFAULT '{}'::jsonb,
  extra                JSONB NOT NULL DEFAULT '{}'::jsonb,

  generated_text       TEXT NOT NULL DEFAULT '',
  salary_range_market  TEXT NOT NULL DEFAULT '',
  evaluated_at         TIMESTAMPTZ,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT job_results_user_url_unique UNIQUE (user_id, url),
  CONSTRAINT job_results_verdict_valid CHECK (verdict IN ('YES', 'MAYBE', 'NO')),
  CONSTRAINT job_results_score_range CHECK (score >= 0 AND score <= 10),
  CONSTRAINT job_results_base_score_range CHECK (base_score >= 0 AND base_score <= 10)
);

CREATE INDEX IF NOT EXISTS job_results_user_score_idx
  ON public.job_results (user_id, score DESC);
CREATE INDEX IF NOT EXISTS job_results_user_verdict_idx
  ON public.job_results (user_id, verdict);
CREATE INDEX IF NOT EXISTS job_results_run_idx
  ON public.job_results (user_id, run_id);

-- ---------------------------------------------------------------------------
-- 3. seen_jobs — deduplication cache
-- ---------------------------------------------------------------------------
-- Per user by construction. job_key is md5(normalised title + company +
-- tracking-stripped url), so the same posting reached by a different URL is
-- still recognised.

CREATE TABLE IF NOT EXISTS public.seen_jobs (
  user_id        UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  job_key        TEXT NOT NULL,
  first_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, job_key)
);

-- ---------------------------------------------------------------------------
-- 4. raw_dumps — pointers to saved connector payloads
-- ---------------------------------------------------------------------------
-- The payload itself belongs in Storage: a dump is 1.5 to 32 MB, which is
-- unpleasant in JSONB. Only the pointer lives here, so a paid Apify run can be
-- replayed later at no cost.

CREATE TABLE IF NOT EXISTS public.raw_dumps (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id       UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  run_id        TEXT NOT NULL DEFAULT '',
  connector     TEXT NOT NULL,
  actor_id      TEXT NOT NULL DEFAULT '',
  apify_run_id  TEXT NOT NULL DEFAULT '',
  items_count   INTEGER NOT NULL DEFAULT 0,
  storage_path  TEXT NOT NULL DEFAULT '',
  saved_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT raw_dumps_user_connector_unique UNIQUE (user_id, connector, run_id)
);

CREATE INDEX IF NOT EXISTS raw_dumps_user_connector_idx
  ON public.raw_dumps (user_id, connector, saved_at DESC);

-- ---------------------------------------------------------------------------
-- 5. applications — the user's own tracking
-- ---------------------------------------------------------------------------
-- Deliberately separate from job_results: a run writes results and never
-- touches this table, so a status the user typed is never overwritten.

CREATE TABLE IF NOT EXISTS public.applications (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id        UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  job_result_id  UUID NOT NULL REFERENCES public.job_results(id) ON DELETE CASCADE,
  status         TEXT NOT NULL DEFAULT '',
  priority       TEXT NOT NULL DEFAULT '',
  target_salary  TEXT NOT NULL DEFAULT '',
  notes          TEXT NOT NULL DEFAULT '',
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT applications_user_job_unique UNIQUE (user_id, job_result_id)
);

CREATE INDEX IF NOT EXISTS applications_user_idx ON public.applications (user_id);

-- Reuses public.set_updated_at(), created by migration 0000.
DROP TRIGGER IF EXISTS applications_set_updated_at ON public.applications;
CREATE TRIGGER applications_set_updated_at
BEFORE UPDATE ON public.applications
FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();

-- ---------------------------------------------------------------------------
-- 6. Grants
-- ---------------------------------------------------------------------------
-- authenticated: the frontend, constrained by the policies below.
-- service_role: the engine backend, which bypasses RLS and filters by user_id
-- in application code instead.

GRANT SELECT, INSERT, UPDATE, DELETE ON public.runs         TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.job_results  TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.seen_jobs    TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.raw_dumps    TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.applications TO authenticated;

GRANT ALL ON public.runs         TO service_role;
GRANT ALL ON public.job_results  TO service_role;
GRANT ALL ON public.seen_jobs    TO service_role;
GRANT ALL ON public.raw_dumps    TO service_role;
GRANT ALL ON public.applications TO service_role;

-- ---------------------------------------------------------------------------
-- 7. Row Level Security
-- ---------------------------------------------------------------------------
-- The security boundary of the whole multi-user design. Without these, any
-- signed-in user could read every other user's results and CV-derived scoring.
--
-- SELECT/UPDATE/DELETE use USING (which rows are visible); INSERT and UPDATE
-- also use WITH CHECK (which rows may be written). Both halves are needed:
-- USING alone would still let a user insert a row owned by someone else.

ALTER TABLE public.runs         ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.job_results  ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.seen_jobs    ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.raw_dumps    ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.applications ENABLE ROW LEVEL SECURITY;

-- runs
DROP POLICY IF EXISTS "Users can view own runs" ON public.runs;
CREATE POLICY "Users can view own runs" ON public.runs
  FOR SELECT TO authenticated USING (auth.uid() = user_id);
DROP POLICY IF EXISTS "Users can insert own runs" ON public.runs;
CREATE POLICY "Users can insert own runs" ON public.runs
  FOR INSERT TO authenticated WITH CHECK (auth.uid() = user_id);
DROP POLICY IF EXISTS "Users can update own runs" ON public.runs;
CREATE POLICY "Users can update own runs" ON public.runs
  FOR UPDATE TO authenticated USING (auth.uid() = user_id) WITH CHECK (auth.uid() = user_id);
DROP POLICY IF EXISTS "Users can delete own runs" ON public.runs;
CREATE POLICY "Users can delete own runs" ON public.runs
  FOR DELETE TO authenticated USING (auth.uid() = user_id);

-- job_results
DROP POLICY IF EXISTS "Users can view own job results" ON public.job_results;
CREATE POLICY "Users can view own job results" ON public.job_results
  FOR SELECT TO authenticated USING (auth.uid() = user_id);
DROP POLICY IF EXISTS "Users can insert own job results" ON public.job_results;
CREATE POLICY "Users can insert own job results" ON public.job_results
  FOR INSERT TO authenticated WITH CHECK (auth.uid() = user_id);
DROP POLICY IF EXISTS "Users can update own job results" ON public.job_results;
CREATE POLICY "Users can update own job results" ON public.job_results
  FOR UPDATE TO authenticated USING (auth.uid() = user_id) WITH CHECK (auth.uid() = user_id);
DROP POLICY IF EXISTS "Users can delete own job results" ON public.job_results;
CREATE POLICY "Users can delete own job results" ON public.job_results
  FOR DELETE TO authenticated USING (auth.uid() = user_id);

-- seen_jobs
DROP POLICY IF EXISTS "Users can view own seen jobs" ON public.seen_jobs;
CREATE POLICY "Users can view own seen jobs" ON public.seen_jobs
  FOR SELECT TO authenticated USING (auth.uid() = user_id);
DROP POLICY IF EXISTS "Users can insert own seen jobs" ON public.seen_jobs;
CREATE POLICY "Users can insert own seen jobs" ON public.seen_jobs
  FOR INSERT TO authenticated WITH CHECK (auth.uid() = user_id);
DROP POLICY IF EXISTS "Users can update own seen jobs" ON public.seen_jobs;
CREATE POLICY "Users can update own seen jobs" ON public.seen_jobs
  FOR UPDATE TO authenticated USING (auth.uid() = user_id) WITH CHECK (auth.uid() = user_id);
DROP POLICY IF EXISTS "Users can delete own seen jobs" ON public.seen_jobs;
CREATE POLICY "Users can delete own seen jobs" ON public.seen_jobs
  FOR DELETE TO authenticated USING (auth.uid() = user_id);

-- raw_dumps
DROP POLICY IF EXISTS "Users can view own raw dumps" ON public.raw_dumps;
CREATE POLICY "Users can view own raw dumps" ON public.raw_dumps
  FOR SELECT TO authenticated USING (auth.uid() = user_id);
DROP POLICY IF EXISTS "Users can insert own raw dumps" ON public.raw_dumps;
CREATE POLICY "Users can insert own raw dumps" ON public.raw_dumps
  FOR INSERT TO authenticated WITH CHECK (auth.uid() = user_id);
DROP POLICY IF EXISTS "Users can update own raw dumps" ON public.raw_dumps;
CREATE POLICY "Users can update own raw dumps" ON public.raw_dumps
  FOR UPDATE TO authenticated USING (auth.uid() = user_id) WITH CHECK (auth.uid() = user_id);
DROP POLICY IF EXISTS "Users can delete own raw dumps" ON public.raw_dumps;
CREATE POLICY "Users can delete own raw dumps" ON public.raw_dumps
  FOR DELETE TO authenticated USING (auth.uid() = user_id);

-- applications
DROP POLICY IF EXISTS "Users can view own applications" ON public.applications;
CREATE POLICY "Users can view own applications" ON public.applications
  FOR SELECT TO authenticated USING (auth.uid() = user_id);
DROP POLICY IF EXISTS "Users can insert own applications" ON public.applications;
CREATE POLICY "Users can insert own applications" ON public.applications
  FOR INSERT TO authenticated WITH CHECK (auth.uid() = user_id);
DROP POLICY IF EXISTS "Users can update own applications" ON public.applications;
CREATE POLICY "Users can update own applications" ON public.applications
  FOR UPDATE TO authenticated USING (auth.uid() = user_id) WITH CHECK (auth.uid() = user_id);
DROP POLICY IF EXISTS "Users can delete own applications" ON public.applications;
CREATE POLICY "Users can delete own applications" ON public.applications
  FOR DELETE TO authenticated USING (auth.uid() = user_id);
