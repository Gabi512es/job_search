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
# Kept out of the repo's own dumps/, so the test never writes there.
os.environ["DUMPS_DIR"] = str(TMP / "dumps")
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
# profile_id became optional so that lovable mode, where it means nothing, does
# not have to send a dummy one. Locally an empty one is still refused, now by
# the path check rather than by pydantic.
check("missing profile_id -> 400, still refused", r.status_code, 400)
check("and names the empty value", "invalid profile_id" in r.json()["detail"], True)


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
section("PROFILE FROM SUPABASE — what the server runs on, built from get-profile")

import json as _json  # noqa: E402

from jobscout.profile import (  # noqa: E402
    EngineProfileError, UserProfile, profile_from_engine,
)

# The fictional committed profile stands in for a stored scoring document, so
# no real profile is read or printed here.
DOCUMENT = _json.loads((REPO / "profiles" / "example.json").read_text(encoding="utf-8"))
CV = "Fictional CV body, supplied by the route rather than read from a file."


def engine_row(**overrides) -> dict:
    """What get-profile returns: Lovable's columns plus the two engine ones."""
    row = {
        "id": "u-1",
        "target_titles": ["AI Engineer"],
        "engine_scoring_profile": _json.loads(_json.dumps(DOCUMENT)),
        "cv_text": CV,
    }
    row.update(overrides)
    return row


def raises(fn) -> str:
    """The message of the EngineProfileError fn raises, or '' if it does not."""
    try:
        fn()
    except EngineProfileError as exc:
        return str(exc)
    return ""


# -- the happy path ---------------------------------------------------------

built = profile_from_engine(engine_row(), user_id="u-1")
check("builds a UserProfile", isinstance(built, UserProfile), True)
check("from the stored document, not from disk", built.profile_id, "example")
check("dimensions come through", built.dimension_keys, ["role_fit", "location"])
check("weights too", built.weights, {"role_fit": 0.6, "location": 0.4})
check("thresholds too", built.thresholds.yes_above, DOCUMENT["thresholds"]["yes_above"])
check("verdict labels too", built.verdict_labels.yes,
      DOCUMENT["verdict_labels"]["yes"])
check("export sheets too", [sheet.name for sheet in built.export.sheets],
      [sheet["name"] for sheet in DOCUMENT["export"]["sheets"]])
check("and a null writing config stays null, not invented",
      built.writing, None)

# The distinction that makes the prompt correct.
check("cv_text from the route lands in cv_text", built.cv_text, CV)
check("candidate_summary is NOT overwritten with the CV",
      built.candidate_summary, DOCUMENT["candidate_summary"])
check("the route's CV wins over any cv_text inside the document",
      built.cv_text != DOCUMENT["cv_text"], True)
check("load_cv returns it without touching the filesystem", built.load_cv(), CV)

# -- cv_path -----------------------------------------------------------------

stale = engine_row(engine_scoring_profile={**DOCUMENT, "cv_path": "profiles/cv/x.txt"})
built_stale = profile_from_engine(stale, user_id="u-1")
check("a cv_path left in the stored document is dropped, not fatal",
      built_stale.cv_path, None)
check("and the CV is still the route's", built_stale.cv_text, CV)

# -- a text column instead of jsonb -----------------------------------------

as_text = engine_row(engine_scoring_profile=_json.dumps(DOCUMENT))
check("engine_scoring_profile as a JSON string is parsed",
      profile_from_engine(as_text, user_id="u-1").profile_id, "example")
check("a string that is not JSON raises",
      "not valid JSON" in raises(
          lambda: profile_from_engine(engine_row(engine_scoring_profile="{oops"),
                                      user_id="u-1")), True)

# -- the null case, which is the normal case for most accounts --------------

for label, row in (
    ("null", engine_row(engine_scoring_profile=None)),
    ("empty object", engine_row(engine_scoring_profile={})),
    ("absent", {k: v for k, v in engine_row().items() if k != "engine_scoring_profile"}),
):
    message = raises(lambda r=row: profile_from_engine(r, user_id="u-1"))
    check(f"{label} engine_scoring_profile raises", bool(message), True)
    check(f"  and {label} explains the billing consequence",
          "already seen" in message, True)
    check(f"  and {label} says what to do", "Store a scoring profile" in message, True)

check("no default profile is invented: the error is the only outcome",
      raises(lambda: profile_from_engine(engine_row(engine_scoring_profile=None),
                                         user_id="u-1")) != "", True)

# -- the CV --------------------------------------------------------------

for label, value in (("missing", None), ("blank", "   "), ("not a string", 42)):
    message = raises(lambda v=value: profile_from_engine(
        engine_row(cv_text=v), user_id="u-1"))
    check(f"{label} cv_text raises rather than scoring on nothing",
          "nonsense" in message, True)

# -- malformed documents ----------------------------------------------------

bad_weights = {**DOCUMENT, "dimensions": [
    {**DOCUMENT["dimensions"][0], "weight": 0.9},
    {**DOCUMENT["dimensions"][1], "weight": 0.9},
]}
check("a document whose weights do not sum to 1 is refused",
      "must sum to 1.0" in raises(lambda: profile_from_engine(
          engine_row(engine_scoring_profile=bad_weights), user_id="u-1")), True)
check("and the error names the user",
      "'u-1'" in raises(lambda: profile_from_engine(
          engine_row(engine_scoring_profile=bad_weights), user_id="u-1")), True)
check("a list instead of an object is refused",
      "expected an object" in raises(lambda: profile_from_engine(
          engine_row(engine_scoring_profile=[1, 2]), user_id="u-1")), True)
check("a row that is not an object at all is refused",
      "not an object" in raises(lambda: profile_from_engine(None, user_id="u-1")), True)


section("RESOLVE_PROFILE — json reads a file, lovable reads the route")


class FakeStore:
    """Only the one method resolve_profile uses."""

    def __init__(self, row=None, error: Exception | None = None):
        self.row, self.error, self.calls = row, error, []

    def get_profile(self, user_id: str):
        self.calls.append(user_id)
        if self.error:
            raise self.error
        return self.row


def with_lovable(store):
    """Point api at the lovable backend and that store, restoring afterwards."""
    previous = (api_module.STORE_BACKEND, api_module.get_store)
    api_module.STORE_BACKEND = "lovable"
    api_module.get_store = lambda: store
    return previous


def restore(previous) -> None:
    api_module.STORE_BACKEND, api_module.get_store = previous


# json mode is untouched: the file is still the source.
profile, user = api_module.resolve_profile("example", None)
check("json mode reads profiles/example.json", profile.profile_id, "example")
check("and defaults user_id to the profile id", user, "example")
check("an explicit user_id is kept", api_module.resolve_profile("example", "u-9")[1], "u-9")

store = FakeStore(row=engine_row())
previous = with_lovable(store)
try:
    profile, user = api_module.resolve_profile("ignored", "u-1")
    check("lovable mode builds from the route", profile.profile_id, "example")
    check("and asks the route for that user", store.calls, ["u-1"])
    check("and returns the user_id it was given", user, "u-1")
    check("the CV comes from the route", profile.cv_text, CV)

    profile2, _ = api_module.resolve_profile("does_not_exist_anywhere", "u-1")
    check("profile_id is ignored in lovable mode", profile2.profile_id, "example")

    r = client.post("/estimate", json={"user_id": "u-1"}, headers=AUTH)
    check("POST /estimate works with no profile_id at all", r.status_code, 200)
    check("and prices the route's profile, rss only, at nothing",
          r.json()["usd_total"], 0.0)

    r = client.post("/estimate", json={"profile_id": "gabriel"}, headers=AUTH)
    check("a request without user_id is refused", r.status_code, 400)
    check("and says user_id is what is missing",
          "user_id is required" in r.json()["detail"], True)
    check("and that no local file is consulted",
          "not from a local file" in r.json()["detail"], True)
finally:
    restore(previous)

previous = with_lovable(FakeStore(row=engine_row(engine_scoring_profile=None)))
try:
    r = client.post("/estimate", json={"user_id": "u-1"}, headers=AUTH)
    check("a user with no scoring profile gets 422, not a stack trace",
          r.status_code, 422)
    check("and the body explains why there is no fallback",
          "already seen" in r.json()["detail"], True)
finally:
    restore(previous)

previous = with_lovable(FakeStore(error=RuntimeError("engine unreachable")))
try:
    r = client.post("/estimate", json={"user_id": "u-1"}, headers=AUTH)
    check("a failing get-profile is a 502, not a 4xx blaming the caller",
          r.status_code, 502)
    check("and reports what failed",
          "engine unreachable" in r.json()["detail"], True)
finally:
    restore(previous)

check("the backend is restored for the rest of the suite",
      api_module.STORE_BACKEND, "json")


# ===========================================================================
section("SELECT — the second pause, as the frontend sees it")

from jobscout.pipeline import AWAITING_SELECTION  # noqa: E402
from jobscout.store.base import RunRecord as _RunRecord  # noqa: E402

selection_calls: list[dict] = []


def collecting_pipeline(profile, store, secrets, opts, **kwargs):
    """Stops at AWAITING_SELECTION when asked, otherwise finishes."""
    selection_calls.append({
        "opts": opts,
        "reuse": {src.type: getattr(src, "reuse_dump", None)
                  for src in profile.enabled_sources()},
        **kwargs})
    status = AWAITING_SELECTION if opts.select_after_collect else "OK"
    store.save_run(_RunRecord(
        run_id=kwargs["run_id"], user_id=kwargs["user_id"],
        profile_id=profile.profile_id, status=status,
        counts={"available_to_score": 435, "fetched": 703}))


api_module.run_pipeline = collecting_pipeline
try:
    r = client.post("/run", json={"profile_id": "gabriel", "reuse_dumps": True,
                                  "select_after_collect": True,
                                  "user_id": "sel-user"}, headers=AUTH)
    run_id = r.json()["run_id"]
    check("POST /run still answers 202", r.status_code, 202)
    check("select_after_collect reaches the pipeline",
          selection_calls[-1]["opts"].select_after_collect, True)

    poll = client.get(f"/run/{run_id}", params={"user_id": "sel-user"}, headers=AUTH)
    body = poll.json()
    check("polling reports the new status", body["status"], AWAITING_SELECTION)
    check("and carries the pool the selector needs",
          body["counts"]["available_to_score"], 435)

    # Resuming. The preflight below refuses without a saved payload, so give
    # this user one: its contents are never read here, only its existence.
    dump_dir = api_module.dumps_dir_for("sel-user")
    dump_dir.mkdir(parents=True, exist_ok=True)
    (dump_dir / "linkedin_apify__gabriel.json").write_text("{}", encoding="utf-8")

    selection_calls.clear()
    r = client.post(f"/run/{run_id}/select",
                    json={"user_id": "sel-user", "max_jobs_to_score": 108},
                    headers=AUTH)
    check("POST /select answers 202", r.status_code, 202)
    check("with the same run_id, not a new one", r.json()["run_id"], run_id)
    check("the chosen number becomes the cap",
          selection_calls[-1]["opts"].max_jobs_to_score, 108)
    check("and the resumed run does NOT pause again",
          selection_calls[-1]["opts"].select_after_collect, False)
    check("dumps are replayed, so Apify is not paid twice",
          selection_calls[-1]["reuse"]["linkedin_apify"], True)
    check("the run reuses this user's own dump directory",
          selection_calls[-1]["opts"].dumps_dir,
          str(api_module.dumps_dir_for("sel-user")))

    # Errors the frontend has to handle.
    r = client.post("/run/nope/select", json={"user_id": "sel-user"}, headers=AUTH)
    check("an unknown run -> 404", r.status_code, 404)

    r = client.post(f"/run/{run_id}/select",
                    json={"user_id": "someone-else"}, headers=AUTH)
    check("another user's run -> 404, not someone else's data", r.status_code, 404)

    # TestClient runs background tasks synchronously, so the resumed run has
    # already finished by now. Either way it is no longer awaiting a choice.
    current = client.get(f"/run/{run_id}", params={"user_id": "sel-user"},
                         headers=AUTH).json()["status"]
    check("the run has left AWAITING_SELECTION", current != AWAITING_SELECTION, True)
    r = client.post(f"/run/{run_id}/select", json={"user_id": "sel-user"},
                    headers=AUTH)
    check("selecting twice -> 409", r.status_code, 409)
    check("and the message names the status it actually found",
          current in r.json()["detail"], True)
    check("and what it expected instead",
          AWAITING_SELECTION in r.json()["detail"], True)

    r = client.post(f"/run/{run_id}/select", json={}, headers=AUTH)
    check("a missing user_id -> 422 from validation", r.status_code, 422)
finally:
    api_module.run_pipeline = real_pipeline

check("/ advertises the new route",
      "/run/{run_id}/select" in client.get("/").json()["routes"], True)


section("DUMPS — one directory per user, checked before promising a resume")

check("each user gets their own directory",
      api_module.dumps_dir_for("u-1") != api_module.dumps_dir_for("u-2"), True)
# A path-like id collapses to a single directory name, so it cannot climb out
# of the dumps root. That the name itself contains dots does not matter; that
# it stays one component does.
escaped = api_module.dumps_dir_for("../../etc")
check("a path-like id stays one directory deep",
      escaped.parent.resolve(), api_module.DUMPS_DIR.resolve())
check("and resolves inside the dumps root",
      str(escaped.resolve()).startswith(str(api_module.DUMPS_DIR.resolve())), True)
check("an empty id still resolves somewhere safe",
      api_module.dumps_dir_for("").name, "anonymous")

# A run waiting for selection whose saved payload is absent must refuse rather
# than silently re-scraping: gabriel has linkedin_apify enabled, and this
# user's dump directory does not exist.
store = api_module.get_store()
store.save_run(_RunRecord(run_id="no-dump", user_id="ghost-user",
                          profile_id="gabriel", status=AWAITING_SELECTION,
                          counts={"available_to_score": 10}))
r = client.post("/run/no-dump/select", json={"user_id": "ghost-user"}, headers=AUTH)
check("a missing payload -> 409, not a silent second scrape", r.status_code, 409)
check("and the message says why",
      "re-scrape" in r.json()["detail"], True)
check("naming the connector whose payload is gone",
      "linkedin_apify" in r.json()["detail"], True)


# ===========================================================================
section("KEEPALIVE — holding the service awake for exactly as long as a run")

import threading as _threading  # noqa: E402
import time as _time  # noqa: E402


class FakeHttpx:
    """Records the self-pings instead of making them."""

    def __init__(self, status=200, error: Exception | None = None):
        self.status, self.error, self.calls = status, error, []
        self._lock = _threading.Lock()

    def get(self, url, **kwargs):
        with self._lock:
            self.calls.append(url)
        if self.error:
            raise self.error
        return type("R", (), {"status_code": self.status})()


def run_keepalive(seconds: float, *, public_url="https://svc.onrender.com",
                  interval=0.02, maximum=10.0, fake=None):
    """Run _keepalive in a thread for `seconds`, then stop it. Returns the fake."""
    fake = fake or FakeHttpx()
    saved = (api_module.PUBLIC_URL, api_module.KEEPALIVE_INTERVAL,
             api_module.KEEPALIVE_MAX, api_module.httpx)
    api_module.PUBLIC_URL = public_url
    api_module.KEEPALIVE_INTERVAL = interval
    api_module.KEEPALIVE_MAX = maximum
    api_module.httpx = fake
    stop = _threading.Event()
    thread = _threading.Thread(
        target=api_module._keepalive, args=(stop, "run-1"), daemon=True)
    try:
        thread.start()
        _time.sleep(seconds)
        stop.set()
        thread.join(timeout=2.0)
        return fake, thread
    finally:
        (api_module.PUBLIC_URL, api_module.KEEPALIVE_INTERVAL,
         api_module.KEEPALIVE_MAX, api_module.httpx) = saved


# -- off by default ---------------------------------------------------------

check("no RENDER_EXTERNAL_URL in this environment, so it is off",
      api_module.PUBLIC_URL, "")
fake, thread = run_keepalive(0.1, public_url="")
check("with no public URL it pings nothing", fake.calls, [])
check("and the thread exits immediately", thread.is_alive(), False)

# -- pinging ----------------------------------------------------------------

fake, thread = run_keepalive(0.12)
check("it pings the PUBLIC url, not localhost",
      all(u == "https://svc.onrender.com/health" for u in fake.calls), True)
check("repeatedly, once per interval", len(fake.calls) >= 2, True)
check("and stops when told to", thread.is_alive(), False)

# -- the bounds -------------------------------------------------------------

fake, thread = run_keepalive(0.15, maximum=0.05)
check("the hard deadline ends it even if nobody stops it",
      thread.is_alive(), False)
check("after at most a couple of pings", len(fake.calls) <= 3, True)

fake, thread = run_keepalive(0.12, fake=FakeHttpx(error=OSError("boom")))
check("a failing ping does not kill the loop", len(fake.calls) >= 2, True)
check("nor propagate out of the thread", thread.is_alive(), False)

# The first ping waits one interval rather than firing at once: stopping
# straight away must produce none at all.
fake, thread = run_keepalive(0.0, interval=5.0)
check("stopping before the first interval pings nothing", fake.calls, [])

# -- bound to the run, by construction --------------------------------------

started: list[str] = []
stopped: list[bool] = []
real_keepalive = api_module._keepalive


def watched_keepalive(stop, run_id):
    started.append(run_id)
    stopped.append(stop.wait(2.0))


api_module._keepalive = watched_keepalive
api_module.run_pipeline = collecting_pipeline
try:
    client.post("/run", json={"profile_id": "gabriel", "reuse_dumps": True,
                              "user_id": "ka-user"}, headers=AUTH)
    check("a run starts one keepalive", len(started), 1)
    check("and stops it when the run ends", stopped, [True])

    # The property that matters: a pipeline that raises still stops it.
    started.clear(); stopped.clear()

    def exploding_pipeline(*a, **k):
        raise RuntimeError("pipeline exploded")

    api_module.run_pipeline = exploding_pipeline
    client.post("/run", json={"profile_id": "gabriel", "reuse_dumps": True,
                              "user_id": "ka-user"}, headers=AUTH)
    check("a pipeline that raises still stops the keepalive", stopped, [True])
    check("so a ping can never outlive the work it was protecting",
          all(stopped), True)
finally:
    api_module._keepalive = real_keepalive
    api_module.run_pipeline = real_pipeline

check("the interval stays under Render's 15-minute threshold",
      float(os.environ.get("KEEPALIVE_INTERVAL_SECONDS", "600")) < 900, True)


# ===========================================================================
section("DOCTOR — the temporary diagnostic, and its own guardrails")

check("GET /doctor needs the key like everything else",
      client.get("/doctor").status_code, 401)
check("so does POST /doctor/keepalive",
      client.post("/doctor/keepalive").status_code, 401)

r = client.get("/doctor", headers=AUTH)
body = r.json()
check("200 with the key", r.status_code, 200)
check("it reports process uptime, which is the evidence",
      isinstance(body["process_uptime_seconds"], float), True)
check("and says whether the keepalive is even enabled here",
      body["public_url_configured"], False)
check("with a verdict that refuses to conclude without it",
      "proves nothing" in body["verdict"], True)

# Bounds on the diagnostic itself.
check("more than 30 minutes is refused",
      client.post("/doctor/keepalive?minutes=45", headers=AUTH).status_code, 422)
check("and so is zero",
      client.post("/doctor/keepalive?minutes=0", headers=AUTH).status_code, 422)

r = client.post("/doctor/keepalive?minutes=25", headers=AUTH)
started = r.json()
check("a valid duration starts it", r.status_code, 200)
check("it says how many pings to expect", started["expected_pings"], 2)
check("and warns when the mechanism is disabled",
      started["public_url_configured"], False)
check("it tells the operator what to do next",
      "Close every tab" in started["next_step"], True)

# Starting twice must not stack threads holding the service awake.
before = _threading.active_count()
for _ in range(3):
    client.post("/doctor/keepalive?minutes=25", headers=AUTH)
check("repeated starts replace rather than stack",
      _threading.active_count() - before <= 1, True)

# The verdict is computed from uptime and pings, so check both branches
# directly rather than waiting 25 minutes for one of them.
saved = (api_module.PUBLIC_URL, api_module._PROCESS_STARTED)
try:
    api_module.PUBLIC_URL = "https://svc.onrender.com"
    api_module._PROCESS_STARTED = _time.monotonic() - 5
    api_module.KEEPALIVE_LOG.clear()
    v = client.get("/doctor", headers=AUTH).json()["verdict"]
    check("a young process means it was restarted",
          "DID spin down" in v, True)
    check("and says what to do instead", "external pinger" in v, True)

    api_module._PROCESS_STARTED = _time.monotonic() - 1500
    v = client.get("/doctor", headers=AUTH).json()["verdict"]
    check("an old process with no pings is inconclusive, not a pass",
          "no ping was recorded" in v, True)

    api_module.KEEPALIVE_LOG.append({"at": "now", "run_id": "doctor",
                                     "status": 200})
    body = client.get("/doctor", headers=AUTH).json()
    check("an old process WITH pings is the positive result",
          "DOES count" in body["verdict"], True)
    check("and it states the condition it cannot verify",
          "nothing else called this service" in body["verdict"], True)
    check("the pings themselves are returned for inspection",
          body["ping_count"], 1)
finally:
    api_module.PUBLIC_URL, api_module._PROCESS_STARTED = saved
    api_module.KEEPALIVE_LOG.clear()

check("the ping log cannot grow without bound",
      api_module.KEEPALIVE_LOG.maxlen, 50)
check("/ marks the routes as temporary",
      [r for r in client.get("/").json()["routes"] if "doctor" in r],
      ["/doctor (temporary)", "/doctor/keepalive (temporary)"])


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
