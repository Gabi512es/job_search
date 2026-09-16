"""LovableEngineStore: mapping, transport and error handling, all mocked.

    python3 tests/test_engine_store.py

No network call is made. A fake HTTP client records every request the store
builds - URL, headers, JSON body - and replies with canned responses, so the
payload mapping, chunking, retries and error translation all run for real.

What this cannot prove is listed at the end: only a call against the real
Lovable routes settles whether the field names match what the server accepts.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import httpx  # noqa: E402

from jobscout.store.base import Application, JobResult, RunRecord  # noqa: E402
from jobscout.store.json_store import JsonStore  # noqa: E402
from jobscout.store.supabase_store import (  # noqa: E402
    DEFAULT_BASE_URL,
    RESULT_FIELDS,
    EngineStoreError,
    LovableEngineStore,
    MissingRouteError,
    SupabaseStore,
)

CHECKS = 0
FAILURES: list[str] = []
KEY = "test-engine-key-not-real"


def check(label: str, actual, expected) -> None:
    global CHECKS
    CHECKS += 1
    if actual == expected:
        print(f"  OK    {label}")
        return
    FAILURES.append(label)
    print(f"  FAIL  {label}")
    print(f"          expected: {str(expected)[:200]!r}")
    print(f"          actual  : {str(actual)[:200]!r}")


def section(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


# ---------------------------------------------------------------------------
# A fake HTTP client
# ---------------------------------------------------------------------------

NOT_JSON = object()   # distinct from None, which is a valid JSON value


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=None):
        self.status_code = status_code
        self._payload = {} if payload is None else payload
        self.text = text if text is not None else "{}"

    def json(self):
        if self._payload is NOT_JSON:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._payload


class FakeClient:
    """Records requests; replies from a queue or a per-route canned answer."""

    def __init__(self, canned=None, queue=None):
        self.requests: list[dict] = []
        self.canned = canned or {}
        self.queue = list(queue or [])
        self.sleeps: list[float] = []

    def post(self, url, json=None, headers=None, timeout=None):
        route = url.rsplit("/", 1)[-1]
        self.requests.append({"url": url, "route": route, "json": json,
                              "headers": headers, "timeout": timeout})
        if self.queue:
            item = self.queue.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return FakeResponse(payload=self.canned.get(route, {}))


def store_with(canned=None, queue=None, **kwargs):
    client = FakeClient(canned, queue)
    store = LovableEngineStore("", KEY, client=client, **kwargs)
    # Retries would otherwise make the suite sleep for seconds.
    import jobscout.store.supabase_store as module
    module.time.sleep = lambda s: client.sleeps.append(s)
    return store, client


# ===========================================================================
section("CONFIG — base URL and key")

check("the production URL is the default",
      LovableEngineStore("", KEY, client=FakeClient()).base_url, DEFAULT_BASE_URL)
check("a custom base URL is honoured, trailing slash stripped",
      LovableEngineStore("https://dev.example.app/", KEY,
                         client=FakeClient()).base_url,
      "https://dev.example.app")

raised = False
try:
    LovableEngineStore("", "")
except EngineStoreError as exc:
    raised = "ENGINE_API_KEY" in str(exc)
check("refuses to build without a key, naming the variable", raised, True)

import os  # noqa: E402

os.environ["ENGINE_API_KEY"] = KEY
os.environ["LOVABLE_ENGINE_BASE_URL"] = "https://from-env.example.app"
check("from_env reads the base URL",
      LovableEngineStore.from_env().base_url, "https://from-env.example.app")
check("and the key", LovableEngineStore.from_env().api_key, KEY)
del os.environ["LOVABLE_ENGINE_BASE_URL"]
check("an unset base URL falls back to production",
      LovableEngineStore.from_env().base_url, DEFAULT_BASE_URL)
del os.environ["ENGINE_API_KEY"]

check("no Supabase client is imported anywhere",
      any(t in (REPO / "jobscout" / "store" / "supabase_store.py")
          .read_text(encoding="utf-8")
          for t in ("create_client", "postgrest", "from supabase")), False)


section("TRANSPORT — every request is well formed")

store, client = store_with({"seen-jobs": {"seen": []}})
store.seen_among("u1", {"k1"})
request = client.requests[0]
check("hits the right route", request["url"],
      f"{DEFAULT_BASE_URL}/api/public/engine/seen-jobs")
check("sends X-Engine-Key", request["headers"]["X-Engine-Key"], KEY)
check("as JSON", request["headers"]["Content-Type"], "application/json")
check("with a timeout", request["timeout"], 30.0)

for method, args in (("seen_among", ("", {"k"})), ("mark_seen", ("", {"k"})),
                     ("known_urls", ("",)), ("save_results", ("", [])),
                     ("get_applications", ("",))):
    raised = False
    try:
        getattr(store, method)(*args)
    except EngineStoreError:
        raised = True
    check(f"{method} rejects a blank user_id", raised, True)


section("SEEN JOBS — read and write")

# MEASURED against the real route: job_keys=[] returns nothing, so
# seen_keys() cannot be answered and must not pretend otherwise.
store, client = store_with({"seen-jobs": {"seen": ["k1", "k2"]}})
raised, message = False, ""
try:
    store.seen_keys("u1")
except MissingRouteError as exc:
    raised, message = True, str(exc)
check("seen_keys raises instead of returning an empty set", raised, True)
check("and explains that the route filters", "filters on the keys given" in message, True)
check("pointing at seen_among", "seen_among" in message, True)
check("without making a request", client.requests, [])

store, client = store_with({"seen-jobs": {"seen": ["k1"]}})
check("seen_among asks only about the keys given",
      store.seen_among("u1", {"k1", "k9"}), {"k1"})
check("and sends them sorted", client.requests[0]["json"]["job_keys"], ["k1", "k9"])
check("seen_among with no keys makes no request",
      store.seen_among("u1", set()) or len(client.requests), 1)

store, client = store_with({"seen-jobs": {"added": 2}})
store.mark_seen("u1", {"kb", "ka"})
body = client.requests[0]["json"]
check("mode is write", body["mode"], "write")
check("keys are sorted for a stable payload", body["job_keys"], ["ka", "kb"])
store.mark_seen("u1", set())
check("marking nothing sends nothing", len(client.requests), 1)


section("SAVE RESULTS — payload matches the route contract")

full = JobResult(
    user_id="u1", run_id="run7", job_key="abc123def456",
    title="Educador/a social", company="Fundació Test", url="https://x/9",
    source="xarxanet", published="2026-09-01", location="Barcelona",
    verdict="MAYBE", score=6.9, base_score=8.4,
    breakdown={"perfil_fit": 8, "location": 9},
    one_liner="Bon encaje.", match_signals=["a"], gaps=["b"],
    red_flags=["[-2.5] homologación"], flags={"catalan_imprescindible": True},
    extra={"tipo_contrato": "indefinido"}, generated_text="Hola...",
    salary_range_market="€18k-22k", evaluated_at="2026-09-12T10:00:00",
)
store, client = store_with({"save-job-results": {"inserted": 1,
                                                 "results": [{"id": "1", "url": "https://x/9"}]}})
check("returns what the server inserted", store.save_results("u1", [full]), 1)
body = client.requests[0]["json"]
check("user_id is sent once at the top level", body["user_id"], "u1")
row = body["results"][0]
check("and NOT repeated on each row", "user_id" in row, False)
check("the row carries exactly the documented fields",
      sorted(row), sorted(RESULT_FIELDS))
check("url is present", row["url"], "https://x/9")
check("score and base_score both travel", (row["score"], row["base_score"]), (6.9, 8.4))
check("breakdown stays an object", row["breakdown"], {"perfil_fit": 8, "location": 9})
check("arrays stay arrays", row["match_signals"], ["a"])
check("flags stay an object", row["flags"], {"catalan_imprescindible": True})
check("extra stays an object", row["extra"], {"tipo_contrato": "indefinido"})
check("the payload is JSON-serialisable", isinstance(json.dumps(body), str), True)

empty_ts = JobResult(user_id="u1", run_id="r", job_key="k", url="https://x/1")
store, client = store_with({"save-job-results": {"inserted": 1}})
store.save_results("u1", [empty_ts])
check("an empty evaluated_at is sent as null, not ''",
      client.requests[0]["json"]["results"][0]["evaluated_at"], None)

store, client = store_with({"save-job-results": {"inserted": 50}})
rows = [JobResult(user_id="u1", run_id="r", job_key=f"k{i}", url=f"https://x/{i}")
        for i in range(120)]
store.save_results("u1", rows)
sizes = [len(r["json"]["results"]) for r in client.requests]
check("large batches are chunked at 50", sizes, [50, 50, 20])
check("every chunk carries the user_id",
      all(r["json"]["user_id"] == "u1" for r in client.requests), True)

store, client = store_with({"save-job-results": {"inserted": 1}})
dupes = [JobResult(user_id="u1", run_id="r", job_key="k", url="https://x/same")
         for _ in range(3)]
store.save_results("u1", dupes)
check("duplicate URLs inside a batch are collapsed",
      len(client.requests[0]["json"]["results"]), 1)

store, client = store_with()
raised = False
try:
    store.save_results("u1", [full, JobResult(user_id="u2", run_id="r",
                                              job_key="k", url="https://x/2")])
except EngineStoreError as exc:
    raised = "u2" in str(exc)
check("a foreign row aborts the batch before any request", raised, True)
check("and nothing was sent", client.requests, [])
check("an empty batch sends nothing", store.save_results("u1", []), 0)

store, client = store_with({"save-job-results": {"results": [{"id": "1"}]}})
check("a response without 'inserted' falls back to counting results",
      store.save_results("u1", [full]), 1)


section("SAVE RUN — payload matches update-run")

record = RunRecord(run_id="r1", user_id="u1", profile_id="gabriel",
                   started_at="2026-09-15T10:00:00", finished_at="",
                   status="RUNNING", cost_decision="", cost_estimate={"usd": 1.2},
                   cost_fingerprint="abc123", counts={"scored": 3})
store, client = store_with({"update-run": {"run": {}}})
store.save_run(record)
body = client.requests[0]["json"]
check("route is update-run", client.requests[0]["route"], "update-run")
check("keys match the documented contract", sorted(body),
      sorted(["run_id", "user_id", "profile_id", "status", "cost_decision",
              "cost_estimate", "cost_fingerprint", "counts",
              "started_at", "finished_at"]))
check("started_at travels", body["started_at"], "2026-09-15T10:00:00")
check("an empty finished_at becomes null", body["finished_at"], None)
check("counts travel as an object", body["counts"], {"scored": 3})
check("the fingerprint travels", body["cost_fingerprint"], "abc123")


section("PROFILE AND RAW DUMP — the two routes with no Store method")

store, client = store_with({"get-profile": {"profile": {"user_id": "u1",
                                                        "target_titles": ["AI Engineer"]}}})
check("get_profile returns the profile object",
      store.get_profile("u1")["target_titles"], ["AI Engineer"])
check("and posts only the user_id", client.requests[0]["json"], {"user_id": "u1"})

store, client = store_with({"save-raw-dump": {"raw_dump": {"id": "d1"}}})
result = store.save_raw_dump("u1", "linkedin_apify", actor_id="curious_coder/x",
                             apify_run_id="run9", items_count=1200,
                             storage_path="dumps/li.json", run_id="r1")
check("save_raw_dump returns the row", result, {"id": "d1"})
body = client.requests[0]["json"]
check("with every documented field", sorted(body),
      sorted(["user_id", "connector", "actor_id", "apify_run_id",
              "items_count", "storage_path", "run_id"]))
check("items_count travels as a number", body["items_count"], 1200)


section("MISSING ROUTES — degrade or fail, never silently wrong")

store, client = store_with()
check("get_applications degrades to empty", store.get_applications("u1"), {})

for method, args, route_hint in (
    ("save_application", (Application(user_id="u1", job_url="https://x/1"),),
     "save-application"),
):
    raised, message = False, ""
    try:
        getattr(store, method)(*args)
    except MissingRouteError as exc:
        raised, message = True, str(exc)
    check(f"{method} raises MissingRouteError", raised, True)
    check(f"{method} names the route Lovable would need to add",
          route_hint in message, True)

check("MissingRouteError is an EngineStoreError",
      issubclass(MissingRouteError, EngineStoreError), True)
check("no request was made by any of them", client.requests, [])

section("GET-RUN — the real route")

client = FakeClient({"get-run": {"run": {
    "run_id": "r1", "user_id": "u1", "profile_id": "gabriel",
    "started_at": "2026-09-15T18:10:28+00:00", "finished_at": None,
    "status": "RUNNING", "cost_decision": "", "cost_estimate": {"usd_total": 0},
    "cost_fingerprint": "abc", "counts": {"scored": 3},
    "created_at": "2026-09-15T16:10:29+00:00"}}})
store = LovableEngineStore("", KEY, client=client)
record = store.get_run("u1", "r1")
check("reads a run", record.status, "RUNNING")
check("posts exactly {user_id, run_id}",
      client.requests[0]["json"], {"user_id": "u1", "run_id": "r1"})
check("on the get-run route", client.requests[0]["route"], "get-run")
check("maps counts", record.counts, {"scored": 3})
check("maps the fingerprint", record.cost_fingerprint, "abc")
check("a null finished_at becomes ''", record.finished_at, "")
check("drops columns the model does not have",
      "created_at" in record.model_dump(), False)

# "not found" is a normal answer to a poll, not a failure.
store, client = store_with(queue=[FakeResponse(404, {"error": "Run not found"})])
check("a JSON 404 returns None rather than raising",
      store.get_run("u1", "nope"), None)

store, client = store_with(queue=[FakeResponse(404, {}, text="<!DOCTYPE html>")])
raised, message = False, ""
try:
    store.get_run("u1", "r1")
except EngineStoreError as exc:
    raised, message = True, str(exc)
check("but an HTML 404 still raises: the route itself is missing",
      raised and "does not exist" in message, True)

store, client = store_with({"get-run": {"run": {}}})
check("an empty run object reads as not found", store.get_run("u1", "r1"), None)

raised = False
try:
    store.get_run("", "r1")
except EngineStoreError:
    raised = True
check("get_run rejects a blank user_id", raised, True)


section("GET-KNOWN-URLS — the dedicated deduplication route")

ROW = {
    "id": "row-uuid", "created_at": "2026-09-16T00:00:00+00:00",
    "user_id": "u1", "run_id": "run1", "job_key": "k1",
    "title": "AI Engineer", "company": "Nova", "url": "https://x/1",
    "source": "hnrss.org", "published": "2026-09-01", "location": "Barcelona",
    "verdict": "MAYBE", "score": "6.9", "base_score": "8.4",
    "breakdown": {"tech_fit": 7}, "one_liner": "Decent.",
    "match_signals": ["Python"], "gaps": [], "red_flags": ["[-1.5] x"],
    "flags": {"probe": True}, "extra": {"cv_patches": "y"},
    "generated_text": "", "salary_range_market": "€60k",
    "evaluated_at": "2026-09-16T10:00:00+00:00",
}

store, client = store_with({"get-known-urls": {
    "urls": ["https://x/1", "https://x/2", "https://x/3"], "count": 3}})
check("known_urls returns the set", store.known_urls("u1"),
      {"https://x/1", "https://x/2", "https://x/3"})
check("on the dedicated route", client.requests[0]["route"], "get-known-urls")
check("posting only the user_id", client.requests[0]["json"], {"user_id": "u1"})
check("and no limit, because the route pages internally",
      "limit" in client.requests[0]["json"], False)
check("exactly one request", len(client.requests), 1)

store, client = store_with({"get-known-urls": {"urls": [], "count": 0}})
check("a user with nothing stored gets an empty set",
      store.known_urls("u1"), set())

store, client = store_with({"get-known-urls": {
    "urls": ["https://x/1", "", None, 42, "https://x/2"], "count": 5}})
check("blank and non-string entries are dropped",
      store.known_urls("u1"), {"https://x/1", "https://x/2"})

# The route reports its own count; a count larger than the list means the list
# was truncated, and every missing URL is a posting billed twice.
store, client = store_with({"get-known-urls": {
    "urls": ["https://x/1", "https://x/2"], "count": 900}})
raised, message = False, ""
try:
    store.known_urls("u1")
except EngineStoreError as exc:
    raised, message = True, str(exc)
check("a count larger than the list raises", raised, True)
check("naming the consequence", "billed again" in message, True)

store, client = store_with({"get-known-urls": {
    "urls": ["https://x/1", "https://x/2"]}})
check("a missing count is tolerated",
      store.known_urls("u1"), {"https://x/1", "https://x/2"})

store, client = store_with({"get-known-urls": {"count": 3}})
raised, message = False, ""
try:
    store.known_urls("u1")
except EngineStoreError as exc:
    raised, message = True, str(exc)
check("a response with no 'urls' list raises", raised, True)
check("rather than silently re-scoring everything",
      "re-score everything" in message, True)

raised = False
try:
    store.known_urls("")
except EngineStoreError:
    raised = True
check("known_urls rejects a blank user_id", raised, True)

check("get-job-results is no longer used for deduplication",
      [r for r in client.requests if r["route"] == "get-job-results"], [])


section("GET-JOB-RESULTS — still serves get_results, whole rows")

store, client = store_with({"get-job-results": {"results": [ROW]}})
store.get_results("u1")
check("get_results uses get-job-results",
      client.requests[0]["route"], "get-job-results")
check("with the maximum the route allows",
      client.requests[0]["json"]["limit"], 500)


section("GET-JOB-RESULTS — row mapping")

store, client = store_with({"get-job-results": {"results": [ROW]}})
results = store.get_results("u1")
check("returns JobResult objects", isinstance(results[0], JobResult), True)
row = results[0]
check("numeric columns are coerced", (row.score, row.base_score), (6.9, 8.4))
check("jsonb arrives as a dict", row.breakdown, {"tech_fit": 7})
check("text[] arrives as a list", row.red_flags, ["[-1.5] x"])
check("extra survives", row.extra, {"cv_patches": "y"})
check("db-only columns are dropped", "id" in row.model_dump(), False)

store, client = store_with({"get-job-results": {"results": [{
    "user_id": "u1", "url": "https://x/1", "verdict": None, "score": None,
    "title": None, "match_signals": None, "breakdown": None,
    "run_id": None, "job_key": None, "evaluated_at": None}]}})
sparse = store.get_results("u1")[0]
check("NULL verdict defaults to NO", sparse.verdict, "NO")
check("NULL numerics become 0.0", sparse.score, 0.0)
check("NULL text becomes ''", sparse.title, "")
check("NULL arrays become []", sparse.match_signals, [])
check("NULL jsonb becomes {}", sparse.breakdown, {})

store, client = store_with({"get-job-results": {"results": [ROW]}})
store.get_results("u1", verdicts=["YES", "MAYBE"])
check("the verdict filter is forwarded",
      client.requests[0]["json"]["verdicts"], ["YES", "MAYBE"])

store, client = store_with({"get-job-results": {"no_results_key": []}})
raised = False
try:
    store.get_results("u1")
except EngineStoreError as exc:
    raised = "expected a 'results' list" in str(exc)
check("a malformed response is reported, not silently empty", raised, True)

raised = False
try:
    store.get_results("")
except EngineStoreError:
    raised = True
check("get_results rejects a blank user_id", raised, True)


section("ERRORS — every failure mode is translated")

for status, fragment, label in (
    (401, "X-Engine-Key", "401 points at the key"),
    (400, "payload was rejected", "400 says the payload was rejected"),
    (404, "unknown to Lovable", "404 on a JSON body means unknown user/record"),
):
    store, client = store_with(queue=[FakeResponse(status, {}, text='{"error":"x"}')])
    raised, message = False, ""
    try:
        store.mark_seen("u1", {"k"})
    except EngineStoreError as exc:
        raised, message = True, str(exc)
    check(label, raised and fragment in message, True)
    check(f"  and names the route ({status})", "seen-jobs" in message, True)
    check(f"  and does not retry a {status}", len(client.requests), 1)

# MEASURED: a missing route falls through to the web app and returns HTML,
# with the same 404 status as an unknown user. The body tells them apart.
store, client = store_with(queue=[FakeResponse(404, {}, text="<!DOCTYPE html><html>")])
raised, message = False, ""
try:
    store.seen_keys_unused if False else store.mark_seen("u1", {"k"})
except EngineStoreError as exc:
    raised, message = True, str(exc)
check("a 404 with an HTML body is reported as a missing route",
      raised and "does not exist" in message, True)

store, client = store_with(queue=[httpx.TimeoutException("slow"),
                                  httpx.TimeoutException("slow"),
                                  httpx.TimeoutException("slow")])
raised, message = False, ""
try:
    store.mark_seen("u1", {"k"})
except EngineStoreError as exc:
    raised, message = True, str(exc)
check("a timeout is retried then reported", raised and "timed out" in message, True)
check("three attempts were made", len(client.requests), 3)
check("with a backoff between them", client.sleeps, [1.5, 3.0])

store, client = store_with(queue=[httpx.TimeoutException("slow"),
                                  FakeResponse(200, {"seen": ["k1"]})])
check("a transient timeout recovers on retry",
      store.seen_among("u1", {"k1"}), {"k1"})

store, client = store_with(queue=[FakeResponse(503, {}, text="upstream down"),
                                  FakeResponse(503, {}, text="upstream down"),
                                  FakeResponse(200, {"seen": ["k2"]})])
check("a 5xx is retried and recovers", store.seen_among("u1", {"k2"}), {"k2"})

store, client = store_with(queue=[FakeResponse(503), FakeResponse(503),
                                  FakeResponse(503)])
raised = False
try:
    store.mark_seen("u1", {"k"})
except EngineStoreError as exc:
    raised = "server error 503" in str(exc)
check("a persistent 5xx is reported after three attempts", raised, True)

store, client = store_with(queue=[httpx.ConnectError("no route to host")] * 3)
raised, message = False, ""
try:
    store.mark_seen("u1", {"k"})
except EngineStoreError as exc:
    raised, message = True, str(exc)
check("a network error names the host", raised and DEFAULT_BASE_URL in message, True)

store, client = store_with(queue=[FakeResponse(200, NOT_JSON, text="<html>nope</html>")])
raised = False
try:
    store.mark_seen("u1", {"k"})
except EngineStoreError as exc:
    raised = "not JSON" in str(exc)
check("a non-JSON body is reported clearly", raised, True)

store, client = store_with(queue=[FakeResponse(200, ["a", "list"])])
raised = False
try:
    store.mark_seen("u1", {"k"})
except EngineStoreError as exc:
    raised = "expected a JSON object" in str(exc)
check("a JSON array instead of an object is rejected", raised, True)

store, client = store_with(queue=[FakeResponse(302, {}, text="moved")])
raised = False
try:
    store.mark_seen("u1", {"k"})
except EngineStoreError as exc:
    raised = "unexpected status 302" in str(exc)
check("an unexpected status is reported", raised, True)


section("PROTOCOL — still interchangeable with JsonStore")

import inspect  # noqa: E402

json_methods = sorted(m for m in dir(JsonStore)
                      if not m.startswith("_") and callable(getattr(JsonStore, m)))
check("every JsonStore method exists here",
      [m for m in json_methods if not hasattr(LovableEngineStore, m)], [])
check("with identical signatures",
      [m for m in json_methods
       if list(inspect.signature(getattr(JsonStore, m)).parameters)
       != list(inspect.signature(getattr(LovableEngineStore, m)).parameters)], [])
check("SupabaseStore is kept as an alias so api.py still imports",
      SupabaseStore is LovableEngineStore, True)

store = JsonStore("/tmp/jobscout_protocol_check")
check("JsonStore is untouched and still works",
      store.save_results("u1", [JobResult(user_id="u1", run_id="r", job_key="k",
                                          url="https://x/1")]), 1)
check("and reads back", len(store.get_results("u1")), 1)
import shutil  # noqa: E402
shutil.rmtree("/tmp/jobscout_protocol_check", ignore_errors=True)

section("NO CREDENTIAL IN THE SOURCE")

source = (REPO / "jobscout" / "store" / "supabase_store.py").read_text(encoding="utf-8")
for secret in ("eyJ", "sk-ant-", "apify_api_", "service_role"):
    check(f"no {secret!r} literal", secret in source, False)
# Twice: the same project id in DEFAULT_BASE_URL and DEV_BASE_URL. A project
# id is not a secret - it is in every frontend bundle - but it should appear
# only as those two named constants.
check("the project id appears only as the two named base URLs",
      source.count("932434d1-f765-4be8-a763-24175ab20d98"), 2)
check("and production is the default",
      source.split("DEFAULT_BASE_URL = (")[1].split(")")[0].count("-dev"), 0)
check("credentials come from the environment",
      "ENGINE_API_KEY" in source and "LOVABLE_ENGINE_BASE_URL" in source, True)


# ===========================================================================
section("WHAT THIS CANNOT PROVE — needs a real call, on your go-ahead")
print("""  1. That the field names above are what the routes actually accept. The
     mapping follows the documented contract, but only a 200 from the server
     confirms it.
  2. That save-job-results returns `inserted` as a count. If it returns
     something else, save_results falls back to len(results), which would
     report differently.
  3. That seen-jobs with job_keys=[] returns the whole cache rather than an
     empty list. If it does not, seen_keys would silently return nothing and
     every posting would be re-scored - the one degradation that costs money.
  4. Whether 404 means "unknown user" or "route not found"; both are reported
     together for now.
  5. Real latency, and whether 30s is enough for a 50-row batch.""")


# ===========================================================================
print()
print("=" * 78)
if FAILURES:
    print(f"FAILED — {len(FAILURES)} of {CHECKS} checks:")
    for f in FAILURES:
        print(f"  - {f}")
    print("=" * 78)
    sys.exit(1)
print(f"PASSED — {CHECKS} checks. No network call was made.")
print("=" * 78)
