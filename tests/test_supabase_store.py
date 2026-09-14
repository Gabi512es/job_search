"""SupabaseStore: what can be checked without a Supabase project.

    python3 tests/test_supabase_store.py

Costs nothing, touches no network. Two halves:

  1. The SQL is parsed as text and checked for the guarantees the design
     depends on - RLS on every table, the unique (user_id, url) constraint,
     foreign keys to auth.users, no credential in the file.
  2. The Python is exercised against a fake PostgREST client that records the
     queries it is asked to build, so filtering, chunking, ownership checks and
     row mapping all run for real.

What this CANNOT prove is listed at the end and needs a real project.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from jobscout.store.base import Application, JobResult, RunRecord  # noqa: E402
from jobscout.store.json_store import JsonStore  # noqa: E402
from jobscout.store.supabase_store import (  # noqa: E402
    SupabaseStore,
    SupabaseStoreError,
    _from_row,
    _to_row,
)

CHECKS = 0
FAILURES: list[str] = []
SQL = (REPO / "supabase" / "schema.sql").read_text(encoding="utf-8")
TABLES = ["profiles", "runs", "seen_jobs", "job_results", "raw_dumps", "applications"]


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
section("SQL — the six tables exist with the documented shape")

for table in TABLES:
    check(f"{table}: created",
          bool(re.search(rf"create table if not exists public\.{table}\b", SQL)), True)
    check(f"{table}: user_id references auth.users",
          bool(re.search(rf"create table if not exists public\.{table}\b.*?"
                         rf"user_id\s+uuid not null references auth\.users",
                         SQL, re.S)), True)

check("job_results has the unique (user_id, url) constraint",
      "unique (user_id, url)" in SQL, True)
check("seen_jobs is keyed on (user_id, job_key)",
      "primary key (user_id, job_key)" in SQL, True)
check("applications reference job_results with cascade",
      "references public.job_results (id) on delete cascade" in SQL, True)
check("deleting a user cascades everywhere",
      SQL.count("references auth.users (id) on delete cascade"), 6)

# Columns of the deliverable table must match the Python model exactly.
block = re.search(r"create table if not exists public\.job_results\s*\((.*?)\n\);",
                  SQL, re.S).group(1)
sql_columns = {c for c in re.findall(r"^\s{4}(\w+)\s+\S", block, re.M)
               if c not in ("constraint",)}   # constraint lines are not columns
model_columns = set(JobResult.model_fields)
check("job_results columns cover every model field",
      sorted(model_columns - sql_columns), [])
check("and add only database-managed ones",
      sorted(sql_columns - model_columns), ["created_at", "id"])

check("verdict is constrained to the three internal values",
      "check (verdict in ('YES', 'MAYBE', 'NO'))" in SQL, True)
check("scores are constrained to 0-10",
      SQL.count("score >= 0 and score <= 10")
      + SQL.count("base_score >= 0 and base_score <= 10"), 2)


section("SQL — row level security, the point of the whole design")

for table in TABLES:
    check(f"{table}: RLS enabled",
          f"alter table public.{table} " in SQL.replace("  ", " ")
          and bool(re.search(rf"alter table public\.{table}\s+enable row level security",
                             SQL.replace("     ", " ").replace("  ", " "))), True)

normalised = re.sub(r"[ \t]+", " ", SQL)
for table in TABLES:
    check(f"{table}: RLS forced (applies to the owner too)",
          f"alter table public.{table} force row level security" in normalised, True)

check("policies restrict reads to the owner", "using (auth.uid() = user_id)" in SQL, True)
check("and writes too", "with check (auth.uid() = user_id)" in SQL, True)
check("policies target authenticated users", "to authenticated" in SQL, True)
check("anon is revoked", bool(re.search(r"revoke all on.*?from anon", SQL, re.S)), True)
check("a policy is created for each of the six tables",
      bool(re.search(r"foreach t in array array\[(.*?)\]", SQL, re.S)), True)
policy_loop = re.search(r"foreach t in array array\[(.*?)\]", SQL, re.S).group(1)
check("the loop covers exactly the six tables",
      sorted(re.findall(r"'(\w+)'", policy_loop)), sorted(TABLES))

section("SQL — no credential, and safe to re-run")

check("no real project URL in the SQL",
      re.findall(r"https://[a-z0-9]{12,}\.supabase\.co", SQL), [])
for secret in ("eyJ", "service_role_key", "sk-", "postgres://"):
    check(f"no {secret!r} literal in the file", secret in SQL, False)
check("every create table is idempotent",
      SQL.count("create table") == SQL.count("create table if not exists"), True)
check("every index is idempotent",
      SQL.count("create index") == SQL.count("create index if not exists"), True)
check("triggers are dropped before being recreated",
      SQL.count("create trigger"), SQL.count("drop trigger if exists"))


# ===========================================================================
section("PYTHON — a fake PostgREST client records what would be sent")


class FakeQuery:
    def __init__(self, table, log):
        self.table, self.log = table, log
        self.filters, self.payload, self.op = {}, None, None
        self.result: list[dict] = []

    def select(self, *cols):
        self.op, self.cols = "select", cols
        return self

    def eq(self, col, val):
        self.filters[col] = val
        return self

    def in_(self, col, vals):
        self.filters[col] = list(vals)
        return self

    def limit(self, n):
        return self

    def order(self, col, desc=False):
        return self

    def upsert(self, rows, on_conflict=None, ignore_duplicates=False):
        self.op = "upsert"
        self.payload = rows if isinstance(rows, list) else [rows]
        self.on_conflict = on_conflict
        self.ignore_duplicates = ignore_duplicates
        return self

    def execute(self):
        self.log.append(self)
        return type("Resp", (), {"data": self.result})()


class FakeClient:
    def __init__(self):
        self.log: list[FakeQuery] = []
        self.canned: dict[str, list[dict]] = {}

    def table(self, name):
        q = FakeQuery(name, self.log)
        q.result = list(self.canned.get(name, []))
        return q


def store_with(canned=None):
    client = FakeClient()
    client.canned = canned or {}
    return SupabaseStore("", "", client=client), client


section("PYTHON — a blank user_id is refused, not treated as 'everyone'")

store, _ = store_with()
for blank in ("", "   ", None):
    raised = False
    try:
        store.seen_keys(blank)
    except SupabaseStoreError:
        raised = True
    check(f"seen_keys rejects {blank!r}", raised, True)
for method in ("known_urls", "get_results"):
    raised = False
    try:
        getattr(store, method)("")
    except SupabaseStoreError:
        raised = True
    check(f"{method} rejects a blank user_id", raised, True)


section("PYTHON — every query filters on user_id")

# Canned rows shaped like what Postgres really returns.
store, client = store_with({
    "seen_jobs": [{"job_key": "k1"}, {"job_key": "k2"}],
    "job_results": [{
        "id": "row-uuid", "created_at": "2026-09-13T00:00:00+00:00",
        "user_id": "u1", "run_id": "run1", "job_key": "k1",
        "title": "AI Engineer", "company": "Nova", "url": "https://x/1",
        "source": "linkedin", "published": "2026-09-01", "location": "Barcelona",
        "verdict": "YES", "score": "8.6", "base_score": "8.6",
        "breakdown": {"tech_fit": 9}, "one_liner": "Good fit.",
        "match_signals": ["Python"], "gaps": [], "red_flags": [],
        "flags": {}, "extra": {}, "generated_text": "",
        "salary_range_market": "", "evaluated_at": "2026-09-12T10:00:00+00:00",
    }],
})
store.seen_keys("u1")
store.known_urls("u1")
store.get_results("u1", verdicts=["YES"])
store.get_run("u1", "run1")
store.get_applications("u1")

check("five reads were issued", len(client.log), 5)
check("all filtered by user_id",
      all(q.filters.get("user_id") == "u1" for q in client.log), True)
check("tables touched",
      sorted({q.table for q in client.log}),
      ["applications", "job_results", "runs", "seen_jobs"])
verdict_query = [q for q in client.log if q.filters.get("verdict")]
check("the verdict filter is applied", verdict_query[0].filters["verdict"], ["YES"])

# A row straight out of Postgres maps cleanly: numerics arrive as strings,
# timestamps carry a timezone, and id/created_at are not model fields.
read_back = store.get_results("u1")[0]
check("numeric columns are coerced to float", read_back.score, 8.6)
check("jsonb arrives as a dict", read_back.breakdown, {"tech_fit": 9})
check("text[] arrives as a list", read_back.match_signals, ["Python"])
check("db-only columns are dropped", read_back.model_dump().get("id", "absent"),
      "absent")
check("applications joins job_results for the URL",
      any("job_results" in str(getattr(q, "cols", "")) for q in client.log
          if q.table == "applications"), True)


section("PYTHON — ownership is checked before anything is written")

store, client = store_with()
mine = JobResult(user_id="u1", run_id="r", job_key="k", url="https://x/1")
theirs = JobResult(user_id="u2", run_id="r", job_key="k", url="https://x/2")

raised = False
try:
    store.save_results("u1", [mine, theirs])
except SupabaseStoreError as exc:
    raised = "u2" in str(exc)
check("a foreign row aborts the batch", raised, True)
check("and nothing was written",
      [q for q in client.log if q.op == "upsert"], [])


section("PYTHON — inserts skip known URLs and chunk large batches")

store, client = store_with({"job_results": [
    {"user_id": "u1", "url": "https://x/1", "verdict": "NO"}]})
rows = [JobResult(user_id="u1", run_id="r", job_key=f"k{i}", url=f"https://x/{i}")
        for i in range(250)]
store.save_results("u1", rows)
upserts = [q for q in client.log if q.op == "upsert" and q.table == "job_results"]
sent = [r for q in upserts for r in q.payload]
check("the already-known URL is skipped",
      [r for r in sent if r["url"] == "https://x/1"], [])
check("the other 249 are sent", len(sent), 249)
check("split into chunks of at most 100", [len(q.payload) for q in upserts],
      [100, 100, 49])
check("upsert targets the unique constraint",
      {q.on_conflict for q in upserts}, {"user_id,url"})
check("and ignores duplicates rather than failing",
      all(q.ignore_duplicates for q in upserts), True)
check("database-managed columns are never sent",
      [k for r in sent for k in ("id", "created_at") if k in r], [])

store, client = store_with()
dupes = [JobResult(user_id="u1", run_id="r", job_key="k", url="https://x/same")
         for _ in range(3)]
store.save_results("u1", dupes)
sent = [r for q in client.log if q.op == "upsert" for r in q.payload]
check("duplicates inside one batch are collapsed", len(sent), 1)

store, client = store_with()
check("an empty batch writes nothing", store.save_results("u1", []), 0)
store.mark_seen("u1", set())
check("marking nothing as seen writes nothing",
      [q for q in client.log if q.op == "upsert"], [])


section("PYTHON — row mapping round-trips")

full = JobResult(
    user_id="u1", run_id="run7", job_key="abc123def456",
    title="Educador/a social", company="Fundació Test", url="https://x/9",
    source="xarxanet", published="2026-09-01", location="Barcelona",
    verdict="MAYBE", score=6.9, base_score=8.4,
    breakdown={"perfil_fit": 8, "location": 9},
    one_liner="Bon encaje.", match_signals=["a", "b"], gaps=["c"],
    red_flags=["[-2.5] homologación"], flags={"catalan_imprescindible": True},
    extra={"tipo_contrato": "indefinido"}, generated_text="Hola...",
    salary_range_market="€18k-22k", evaluated_at="2026-09-12T10:00:00",
)
row = _to_row(full)
check("no database-managed column in the row",
      [k for k in ("id", "created_at") if k in row], [])
check("text[] columns stay lists", isinstance(row["match_signals"], list), True)
check("jsonb columns stay dicts", isinstance(row["breakdown"], dict), True)
back = _from_row(dict(row, id="uuid", created_at="2026-09-13T00:00:00"))
check("round-trips to an identical model", back, full)

sparse = _from_row({"user_id": "u1", "url": "https://x/1", "verdict": "NO",
                    "score": None, "base_score": None, "title": None,
                    "match_signals": None, "breakdown": None,
                    "evaluated_at": None, "run_id": None, "job_key": None})
check("NULL numerics become 0.0", sparse.score, 0.0)
check("NULL text becomes ''", sparse.title, "")
check("NULL arrays become []", sparse.match_signals, [])
check("NULL jsonb becomes {}", sparse.breakdown, {})

check("an empty evaluated_at is sent as NULL, not ''",
      _to_row(JobResult(user_id="u", run_id="r", job_key="k",
                        url="https://x"))["evaluated_at"], None)


section("PYTHON — applications resolve through job_results")

store, client = store_with({"job_results": []})
raised = False
try:
    store.save_application(Application(user_id="u1", job_url="https://missing"))
except SupabaseStoreError as exc:
    raised = "no job_result" in str(exc)
check("tracking an unknown result fails loudly", raised, True)

store, client = store_with({"job_results": [{"id": "res-uuid"}]})
store.save_application(Application(user_id="u1", job_url="https://x/1",
                                   status="Entrevista", notes="lunes"))
written = [q for q in client.log if q.op == "upsert"][0]
check("the application is linked by job_result_id",
      written.payload[0]["job_result_id"], "res-uuid")
check("status is stored", written.payload[0]["status"], "Entrevista")
check("upsert keyed on (user_id, job_result_id)",
      written.on_conflict, "user_id,job_result_id")

store, client = store_with({"applications": [
    {"user_id": "u1", "status": "Entrevista", "priority": "Alta",
     "target_salary": "", "notes": "lunes", "updated_at": "2026-09-13T10:00:00",
     "job_results": {"url": "https://x/1"}}]})
apps = store.get_applications("u1")
check("applications come back keyed by URL", list(apps), ["https://x/1"])
check("with their status", apps["https://x/1"].status, "Entrevista")


section("PYTHON — the two stores implement the same protocol")

protocol_methods = [m for m in dir(JsonStore)
                    if not m.startswith("_") and callable(getattr(JsonStore, m))]
missing = [m for m in protocol_methods if not hasattr(SupabaseStore, m)]
check("SupabaseStore implements every JsonStore method", missing, [])
check("JsonStore is untouched and still usable",
      isinstance(JsonStore("/tmp/x").base_dir, Path), True)

import inspect  # noqa: E402
for method in protocol_methods:
    j = list(inspect.signature(getattr(JsonStore, method)).parameters)
    s = list(inspect.signature(getattr(SupabaseStore, method)).parameters)
    check(f"{method}() has the same parameters in both", s, j)

section("PYTHON — no credential is hardcoded")

source = (REPO / "jobscout" / "store" / "supabase_store.py").read_text(encoding="utf-8")
# A real project URL is https://<20-char ref>.supabase.co; the docstring's
# "https://<ref>.supabase.co" placeholder must not trip this.
check("no real project URL in the module",
      re.findall(r"https://[a-z0-9]{12,}\.supabase\.co", source), [])
for secret in ("eyJ", "sk-ant-", "apify_api_"):   # JWT prefix, API key prefixes
    check(f"no {secret!r} literal in the module", secret in source, False)
check("credentials are read from the environment",
      all(v in source for v in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY",
                                "SUPABASE_ANON_KEY")), True)
raised = False
try:
    SupabaseStore("", "")
except SupabaseStoreError as exc:
    raised = "SUPABASE_URL" in str(exc)
check("constructing without credentials fails with a useful message", raised, True)


# ===========================================================================
section("WHAT THIS TEST CANNOT PROVE — needs a real Supabase project")
print("""  1. That the SQL actually applies. It is checked as text here, never run.
  2. That the RLS policies isolate users in practice. Proving that needs two
     real accounts, each querying with its own JWT, and checking neither can
     see the other's rows. This is the single most important thing to verify.
  3. That supabase-py's query builder behaves like the fake used above -
     particularly `upsert(..., ignore_duplicates=True)` returning only the rows
     it actually inserted, which is what save_results() counts.
  4. That the `job_results!inner(url)` join syntax returns the nested shape
     get_applications() expects.
  5. Timestamp handling: Postgres timestamptz in, ISO string out.
  6. Behaviour under a real network: timeouts, retries, rate limits.""")


# ===========================================================================
print()
print("=" * 78)
if FAILURES:
    print(f"FAILED — {len(FAILURES)} of {CHECKS} checks:")
    for f in FAILURES:
        print(f"  - {f}")
    print("=" * 78)
    sys.exit(1)
print(f"PASSED — {CHECKS} checks. No network, no Supabase project needed.")
print("=" * 78)
