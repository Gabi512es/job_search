"""The Lovable migration SQL, checked as text.

    python3 tests/test_migration_sql.py

supabase/lovable_migrations/0002_engine_tables.sql is what actually gets
applied, by being dropped into the Lovable repo's drizzle/migrations/. It is
never run from here, so this checks the guarantees the design depends on:
the five tables, the unique (user_id, url) constraint, RLS on every table with
both USING and WITH CHECK, the grants the engine backend needs, and that it
does not touch the profiles table Lovable already owns.

Replaces the Python half of the old test_supabase_store.py, which covered a
direct-Postgres store that no longer exists. That store now speaks HTTP and is
covered by tests/test_engine_store.py.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from jobscout.store.base import JobResult  # noqa: E402

SQL_PATH = REPO / "supabase" / "lovable_migrations" / "0002_engine_tables.sql"
SQL = SQL_PATH.read_text(encoding="utf-8")
# Statements only: the header comment mentions some of these strings too.
BODY = "\n".join(l for l in SQL.splitlines() if not l.lstrip().startswith("--"))
TABLES = ["runs", "job_results", "seen_jobs", "raw_dumps", "applications"]

CHECKS = 0
FAILURES: list[str] = []


def check(label: str, actual, expected) -> None:
    global CHECKS
    CHECKS += 1
    if actual == expected:
        print(f"  OK    {label}")
        return
    FAILURES.append(label)
    print(f"  FAIL  {label}")
    print(f"          expected: {str(expected)[:180]!r}")
    print(f"          actual  : {str(actual)[:180]!r}")


def section(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


# ===========================================================================
section("SCOPE — the five engine tables, and nothing Lovable already owns")

created = re.findall(r"CREATE TABLE IF NOT EXISTS public\.(\w+)", BODY)
check("creates exactly the five engine tables", sorted(created), sorted(TABLES))
check("never creates, alters or drops profiles",
      bool(re.search(r"(CREATE|ALTER|DROP)\s+TABLE[^;]*public\.profiles", BODY, re.I)),
      False)
check("defines no policy on profiles",
      bool(re.search(r"POLICY[^;]*ON public\.profiles", BODY, re.I)), False)
check("reuses Lovable's set_updated_at() from migration 0000",
      "EXECUTE FUNCTION public.set_updated_at()" in BODY, True)
check("and does not redefine it", "CREATE OR REPLACE FUNCTION" in BODY, False)


section("OWNERSHIP — every table is keyed to a real user")

check("all five reference auth.users with cascade",
      BODY.count("REFERENCES auth.users(id) ON DELETE CASCADE"), 5)
check("applications cascade from job_results",
      "REFERENCES public.job_results(id) ON DELETE CASCADE" in BODY, True)


section("CONSTRAINTS — what stops double billing and bad data")

check("unique (user_id, url) on job_results", "UNIQUE (user_id, url)" in BODY, True)
check("seen_jobs keyed on (user_id, job_key)",
      "PRIMARY KEY (user_id, job_key)" in BODY, True)
check("verdict restricted to the three internal values",
      "CHECK (verdict IN ('YES', 'MAYBE', 'NO'))" in BODY, True)
check("scores bounded 0-10",
      BODY.count("score >= 0 AND score <= 10")
      + BODY.count("base_score >= 0 AND base_score <= 10"), 2)
for status in ("RUNNING", "FAILED", "NEEDS_CONFIRMATION", "REJECTED_OVER_HARD_CAP"):
    check(f"runs.status accepts {status}", f"'{status}'" in BODY, True)


section("SHAPE — job_results matches the Python model exactly")

block = re.search(r"CREATE TABLE IF NOT EXISTS public\.job_results\s*\((.*?)\n\);",
                  BODY, re.S).group(1)
columns = {c for c in re.findall(r"^\s{2}(\w+)\s+\S", block, re.M) if c != "CONSTRAINT"}
model = set(JobResult.model_fields)
check("every model field has a column", sorted(model - columns), [])
check("and only id/created_at are extra", sorted(columns - model), ["created_at", "id"])


section("RLS — the security boundary of the multi-user design")

for table in TABLES:
    check(f"{table}: RLS enabled",
          bool(re.search(rf"ALTER TABLE public\.{table}\s+ENABLE ROW LEVEL SECURITY",
                         BODY)), True)
    policies = re.findall(rf"ON public\.{table}\n  FOR (\w+)", BODY)
    check(f"{table}: one policy per command", sorted(policies),
          ["DELETE", "INSERT", "SELECT", "UPDATE"])

n_create = len(re.findall(r"^CREATE POLICY", BODY, re.M))
n_drop = len(re.findall(r"^DROP POLICY IF EXISTS", BODY, re.M))
check("20 policies in total", n_create, 20)
check("each preceded by a DROP IF EXISTS, so the file can be re-run",
      n_drop, n_create)
check("reads are restricted to the owner",
      BODY.count("USING (auth.uid() = user_id)"), 15)   # SELECT+UPDATE+DELETE
check("writes are too: INSERT and UPDATE both carry WITH CHECK",
      BODY.count("WITH CHECK (auth.uid() = user_id)"), 10)


section("GRANTS — without these the engine backend cannot write")

check("authenticated gets CRUD on all five",
      len(re.findall(r"GRANT SELECT, INSERT, UPDATE, DELETE ON public\.\w+\s+TO authenticated;",
                     BODY)), 5)
check("service_role gets all on all five",
      len(re.findall(r"GRANT ALL ON public\.\w+\s+TO service_role;", BODY)), 5)


section("IDEMPOTENCE — safe to apply twice")

check("every CREATE TABLE is guarded",
      BODY.count("CREATE TABLE") == BODY.count("CREATE TABLE IF NOT EXISTS"), True)
check("every CREATE INDEX is guarded",
      BODY.count("CREATE INDEX") == BODY.count("CREATE INDEX IF NOT EXISTS"), True)
check("the trigger is dropped before being recreated",
      BODY.count("CREATE TRIGGER"), BODY.count("DROP TRIGGER IF EXISTS"))


section("NO CREDENTIAL, NO PERSONAL DATA")

# The forbidden names are read from the gitignored CV files rather than written
# here: hardcoding them would put the very strings the git history was purged
# of back into a tracked file. On a fresh clone the CVs are absent and that
# part is skipped, which is stated rather than silently passed.
def cv_names() -> list[str]:
    """Distinctive capitalised words from the real CVs, if they are present."""
    words: set[str] = set()
    for cv in (REPO / "profiles" / "cv").glob("*.txt"):
        for token in re.findall(r"\b[A-ZÁÉÍÓÚÑÀÈÌÒÙ][\wÁÉÍÓÚÑáéíóúñçÇ'-]{3,}", 
                                cv.read_text(encoding="utf-8")):
            words.add(token)
    return sorted(words)

names = cv_names()
if names:
    leaked = sorted({n for n in names if re.search(rf"\b{re.escape(n)}\b", SQL)})
    check(f"none of the {len(names)} CV words appear in the SQL", leaked, [])
else:
    print("  SKIP  no CV files present, cannot check against the real names")

for pattern, name in (
    (r"eyJ[A-Za-z0-9_-]{20,}", "a JWT"),
    (r"sk-ant-", "an Anthropic key"),
    (r"apify_api_", "an Apify token"),
    (r"https://[a-z0-9]{12,}\.supabase\.co", "a project URL"),
    (r"postgres://", "a connection string"),
    (r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "an email address"),
):
    check(f"contains no {name}", bool(re.search(pattern, SQL)), False)


# ===========================================================================
print()
print("=" * 78)
if FAILURES:
    print(f"FAILED — {len(FAILURES)} of {CHECKS} checks:")
    for f in FAILURES:
        print(f"  - {f}")
    print("=" * 78)
    sys.exit(1)
print(f"PASSED — {CHECKS} checks on {SQL_PATH.name}. The SQL is never executed here.")
print("=" * 78)
