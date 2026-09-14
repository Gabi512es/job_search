"""The HTTP API, exercised in-process. Spends nothing.

    python3 tests/test_api.py

Uses FastAPI's TestClient, so routing, validation, auth and error handling all
run for real without a server or a network. The one thing deliberately never
triggered is a successful POST /run against the real pipeline: that would call
Haiku. The background task is replaced by a recorder instead, which proves the
route hands the right arguments over without executing them.
"""

from __future__ import annotations

import importlib
import os
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

TMP = Path("/private/tmp/claude-501/-Users-gabrielernoult-Desktop-GIT-Repos-Job-Search"
           "/a697afae-6c9e-4187-870d-45749f73a1a8/scratchpad/api")
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)

# Configure before importing api.py: it reads the environment at import time.
API_KEY = "test-key-not-a-real-secret"
os.environ["JOBSCOUT_API_KEY"] = API_KEY
os.environ["STORE_DIR"] = str(TMP / "store")
os.environ["STORE_BACKEND"] = "json"
os.environ["ALLOWED_ORIGINS"] = "https://example.lovable.app,https://other.app"

from fastapi.testclient import TestClient  # noqa: E402

import api as api_module  # noqa: E402

importlib.reload(api_module)
client = TestClient(api_module.app)
AUTH = {"X-API-Key": API_KEY}

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
    print(f"          expected: {str(expected)[:200]!r}")
    print(f"          actual  : {str(actual)[:200]!r}")


def section(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


# ===========================================================================
section("HEALTH — what Railway polls, unauthenticated")

response = client.get("/health")
body = response.json()
check("200", response.status_code, 200)
check("status ok", body["status"], "ok")
check("no auth needed", "X-API-Key" not in str(response.request.headers), True)
check("reports the store backend", body["store_backend"], "json")
check("lists the available profiles",
      sorted(body["profiles_available"]), ["camila", "example", "gabriel"])
check("says the api key is configured", body["config"]["api_key_set"], True)
check("and how many CORS origins", body["config"]["cors_origins"], 2)

# The health payload must never leak a credential value.
for secret in ("sk-ant-", "apify_api_", API_KEY):
    check(f"health body contains no {secret!r}", secret in response.text, False)

check("root route describes the service", client.get("/").status_code, 200)
check("and says runs are asynchronous",
      "asynchronous" in client.get("/").json()["runs_are"], True)


section("AUTH — protected routes reject a missing or wrong key")

for method, path, payload in (
    ("post", "/estimate", {"profile_id": "gabriel"}),
    ("post", "/run", {"profile_id": "gabriel"}),
    ("get", "/run/abc?user_id=gabriel", None),
    ("get", "/results?user_id=gabriel", None),
):
    call = getattr(client, method)
    r = call(path, json=payload) if payload else call(path)
    check(f"{method.upper()} {path.split('?')[0]} without a key -> 401",
          r.status_code, 401)
    r = call(path, json=payload, headers={"X-API-Key": "wrong"}) if payload \
        else call(path, headers={"X-API-Key": "wrong"})
    check(f"{method.upper()} {path.split('?')[0]} with a wrong key -> 401",
          r.status_code, 401)

# With no key configured at all, protected routes refuse rather than open up.
saved = api_module.API_KEY
api_module.API_KEY = ""
check("an unconfigured server returns 503, not open access",
      client.post("/estimate", json={"profile_id": "gabriel"}).status_code, 503)
check("and says why",
      "not configured" in client.post("/estimate",
                                      json={"profile_id": "gabriel"}).text, True)
check("/health still answers", client.get("/health").status_code, 200)
api_module.API_KEY = saved


section("PROFILES — clean errors, not stack traces")

r = client.post("/estimate", json={"profile_id": "does_not_exist"}, headers=AUTH)
check("unknown profile -> 404", r.status_code, 404)
check("and lists what exists", "gabriel" in r.json()["detail"], True)

for bad in ("../secrets", "a/b", ".hidden"):
    r = client.post("/estimate", json={"profile_id": bad}, headers=AUTH)
    check(f"path-like profile_id {bad!r} -> 400", r.status_code, 400)

r = client.post("/estimate", json={}, headers=AUTH)
check("missing profile_id -> 422 from validation", r.status_code, 422)


section("ESTIMATE — real cost figures, nothing spent")

r = client.post("/estimate", json={"profile_id": "gabriel"}, headers=AUTH)
body = r.json()
check("200", r.status_code, 200)
check("gabriel's LinkedIn run is held for confirmation",
      body["decision"], "NEEDS_CONFIRMATION")
check("at the measured $1.20", round(body["usd_total"], 2), 1.20)
check("with a fingerprint to confirm with", bool(body["fingerprint"]), True)
check("and a per-connector breakdown", len(body["per_connector"]), 1)
check("the note explains what is not included",
      "Haiku" in body["note"], True)

r = client.post("/estimate", json={"profile_id": "camila"}, headers=AUTH)
check("camila's InfoJobs run prices at $1.38",
      round(r.json()["usd_total"], 2), 1.38)

r = client.post("/estimate",
                json={"profile_id": "gabriel", "reuse_dumps": True}, headers=AUTH)
check("replaying dumps costs nothing", r.json()["usd_total"], 0.0)
check("and is approved immediately", r.json()["decision"], "OK")

fingerprint = client.post("/estimate", json={"profile_id": "camila"},
                          headers=AUTH).json()["fingerprint"]
r = client.post("/estimate",
                json={"profile_id": "camila", "confirmed_fingerprint": fingerprint},
                headers=AUTH)
check("a confirmed estimate turns OK", r.json()["decision"], "OK")


section("RUN — accepted asynchronously, without executing the pipeline")

# The pipeline is replaced so nothing is billed. What is under test is that the
# route records the run, returns a poll URL, and passes the right arguments.
calls: list[dict] = []
real_pipeline = api_module.run_pipeline


def fake_pipeline(profile, store, secrets, opts, **kwargs):
    calls.append({"profile_id": profile.profile_id, "opts": opts, **kwargs})
    from jobscout.store.base import RunRecord
    store.save_run(RunRecord(
        run_id=kwargs["run_id"], user_id=kwargs["user_id"],
        profile_id=profile.profile_id, status="OK",
        counts={"scored": 3, "saved": 3}))


api_module.run_pipeline = fake_pipeline
try:
    r = client.post("/run", json={"profile_id": "gabriel", "reuse_dumps": True,
                                  "max_jobs_to_score": 3}, headers=AUTH)
    body = r.json()
    check("202 Accepted, not 200", r.status_code, 202)
    check("status is RUNNING", body["status"], "RUNNING")
    check("a run_id is returned", len(body["run_id"]), 12)
    check("with the URL to poll", body["poll"], f"/run/{body['run_id']}")

    check("the pipeline was called once", len(calls), 1)
    check("with the requested profile", calls[0]["profile_id"], "gabriel")
    check("the cap was passed through", calls[0]["opts"].max_jobs_to_score, 3)
    check("the run_id was passed in, not generated inside",
          calls[0]["run_id"], body["run_id"])
    check("user_id defaults to the profile id", calls[0]["user_id"], "gabriel")

    run_id = body["run_id"]
    r = client.get(f"/run/{run_id}?user_id=gabriel", headers=AUTH)
    check("polling returns the finished state", r.json()["status"], "OK")
    check("with its counts", r.json()["counts"]["scored"], 3)

    r = client.post("/run", json={"profile_id": "camila", "user_id": "u-42",
                                  "reuse_dumps": True}, headers=AUTH)
    check("an explicit user_id is honoured", calls[1]["user_id"], "u-42")
    check("and the run is readable under it",
          client.get(f"/run/{r.json()['run_id']}?user_id=u-42",
                     headers=AUTH).status_code, 200)
    check("but not under another user",
          client.get(f"/run/{r.json()['run_id']}?user_id=someone-else",
                     headers=AUTH).status_code, 404)
finally:
    api_module.run_pipeline = real_pipeline

check("an unknown run -> 404",
      client.get("/run/nope?user_id=gabriel", headers=AUTH).status_code, 404)
check("polling without user_id -> 422",
      client.get("/run/nope", headers=AUTH).status_code, 422)


section("RUN — a failing background task is recorded, not swallowed")

def exploding_pipeline(*a, **kw):
    raise RuntimeError("connector exploded")


api_module.run_pipeline = exploding_pipeline
try:
    r = client.post("/run", json={"profile_id": "gabriel", "reuse_dumps": True},
                    headers=AUTH)
    run_id = r.json()["run_id"]
    status = client.get(f"/run/{run_id}?user_id=gabriel", headers=AUTH).json()
    check("the run is marked FAILED", status["status"], "FAILED")
    check("and carries the reason",
          "connector exploded" in status["cost_decision"], True)
finally:
    api_module.run_pipeline = real_pipeline


section("RESULTS — the deliverable")

from jobscout.store.base import JobResult  # noqa: E402

store = api_module.get_store()
store.save_results("gabriel", [
    JobResult(user_id="gabriel", run_id="r1", job_key="k1", title="Top",
              url="https://x/1", verdict="YES", score=9.0),
    JobResult(user_id="gabriel", run_id="r1", job_key="k2", title="Mid",
              url="https://x/2", verdict="MAYBE", score=6.0),
    JobResult(user_id="gabriel", run_id="r1", job_key="k3", title="Low",
              url="https://x/3", verdict="NO", score=2.0),
])

body = client.get("/results?user_id=gabriel", headers=AUTH).json()
check("all three come back", body["total"], 3)
check("sorted by score, best first",
      [r["score"] for r in body["results"]], [9.0, 6.0, 2.0])
check("rows carry the full job_results shape",
      "breakdown" in body["results"][0], True)

body = client.get("/results?user_id=gabriel&verdict=YES", headers=AUTH).json()
check("filtered by verdict", [r["title"] for r in body["results"]], ["Top"])
body = client.get("/results?user_id=gabriel&verdict=YES&verdict=MAYBE",
                  headers=AUTH).json()
check("the filter repeats", body["total"], 2)
body = client.get("/results?user_id=gabriel&limit=1", headers=AUTH).json()
check("limit caps the payload", body["returned"], 1)
check("while reporting the real total", body["total"], 3)
check("another user sees nothing",
      client.get("/results?user_id=stranger", headers=AUTH).json()["total"], 0)
check("limit is bounded",
      client.get("/results?user_id=gabriel&limit=9999", headers=AUTH).status_code, 422)


section("CORS — only the configured origins")

r = client.options("/results", headers={
    "Origin": "https://example.lovable.app",
    "Access-Control-Request-Method": "GET",
    "Access-Control-Request-Headers": "X-API-Key",
})
check("a configured origin is allowed",
      r.headers.get("access-control-allow-origin"), "https://example.lovable.app")
check("X-API-Key is an allowed header",
      "x-api-key" in r.headers.get("access-control-allow-headers", "").lower(), True)

r = client.options("/results", headers={
    "Origin": "https://evil.example.com",
    "Access-Control-Request-Method": "GET",
})
check("an unknown origin gets no allow-origin header",
      r.headers.get("access-control-allow-origin"), None)


# ===========================================================================
print()
print("=" * 78)
if FAILURES:
    print(f"FAILED — {len(FAILURES)} of {CHECKS} checks:")
    for f in FAILURES:
        print(f"  - {f}")
    print("=" * 78)
    sys.exit(1)
print(f"PASSED — {CHECKS} checks. No Anthropic call, no Apify call, no server.")
print("=" * 78)
