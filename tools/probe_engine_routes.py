"""Probe the five Lovable engine routes for real. Read-first, writes marked.

    ENGINE_API_KEY=... python3 -u tools/probe_engine_routes.py <user_id>
    ENGINE_API_KEY=... python3 -u tools/probe_engine_routes.py <user_id> --dev
    ENGINE_API_KEY=... python3 -u tools/probe_engine_routes.py <user_id> --no-writes

Costs nothing in Anthropic or Apify terms: it only talks to Lovable.

It DOES write. Three routes have no read-only mode, so proving their contract
means creating rows. Everything written is prefixed so it can be found and
deleted afterwards, and the script prints exactly what it created:

    run_id       JOBSCOUT-PROBE-<timestamp>
    job url      https://example.invalid/jobscout-probe-<timestamp>
    job_key      JOBSCOUT-PROBE-<timestamp>
    connector    jobscout-probe

Pass --no-writes to run only the read probes.

Question 1 is the one that matters. Everything else is contract checking; that
one decides whether every posting gets re-scored on every run, which is the
only failure here that costs real money.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime

import httpx

PROD = "https://project--932434d1-f765-4be8-a763-24175ab20d98.lovable.app"
DEV = "https://project--932434d1-f765-4be8-a763-24175ab20d98-dev.lovable.app"

STAMP = datetime.now().strftime("%Y%m%d-%H%M%S")
MARKER = f"JOBSCOUT-PROBE-{STAMP}"
PROBE_URL = f"https://example.invalid/jobscout-probe-{STAMP}"

findings: list[tuple[str, str]] = []


def record(label: str, verdict: str) -> None:
    findings.append((label, verdict))


def section(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def call(base: str, key: str, route: str, payload: dict,
         timeout: float = 30.0) -> tuple[int, object, float]:
    """One request. Returns (status, parsed body or raw text, seconds)."""
    url = f"{base}/api/public/engine/{route}"
    started = time.monotonic()
    try:
        response = httpx.post(
            url, json=payload,
            headers={"X-Engine-Key": key, "Content-Type": "application/json"},
            timeout=timeout,
        )
    except Exception as exc:
        return -1, f"{type(exc).__name__}: {exc}", time.monotonic() - started
    elapsed = time.monotonic() - started
    try:
        return response.status_code, response.json(), elapsed
    except Exception:
        return response.status_code, response.text[:400], elapsed


def show(route: str, payload: dict, status: int, body, elapsed: float,
         hide: tuple[str, ...] = ()) -> None:
    shown = {k: ("<%d items>" % len(v) if k in hide and isinstance(v, list) else v)
             for k, v in payload.items()}
    print(f"\n  POST /{route}")
    print(f"    ->  {json.dumps(shown, ensure_ascii=False)[:180]}")
    print(f"    <-  HTTP {status}  ({elapsed:.2f}s)")
    rendered = json.dumps(body, ensure_ascii=False) if not isinstance(body, str) else body
    print(f"        {rendered[:500]}")


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    if not args:
        print("usage: probe_engine_routes.py <user_id> [--dev] [--no-writes]")
        return 2

    user_id = args[0]
    base = DEV if "--dev" in flags else PROD
    do_writes = "--no-writes" not in flags
    key = os.environ.get("ENGINE_API_KEY", "").strip()
    if not key:
        print("ENGINE_API_KEY is not set. Nothing was called.")
        return 2

    print(f"base     : {base}")
    print(f"user_id  : {user_id}")
    print(f"writes   : {'yes, marked ' + MARKER if do_writes else 'no'}")

    # =======================================================================
    section("QUESTION 1 (PRIORITY) — does seen-jobs with job_keys=[] "
            "return the whole cache?")
    print("""  If it returns every known job_key, seen_keys() works and deduplication
  holds. If it returns [], seen_keys() reports 'nothing seen' on every run and
  EVERY POSTING IS RE-SCORED, at roughly $0.005 each. That is the only failure
  here that costs money.""")

    status, body, elapsed = call(base, key, "seen-jobs",
                                 {"user_id": user_id, "mode": "read", "job_keys": []})
    show("seen-jobs", {"user_id": user_id, "mode": "read", "job_keys": []},
         status, body, elapsed)

    empty_seen = None
    if status == 200 and isinstance(body, dict):
        empty_seen = body.get("seen")
        n = len(empty_seen) if isinstance(empty_seen, list) else None
        if n is None:
            record("seen-jobs job_keys=[]", "NO 'seen' KEY IN RESPONSE — unusable")
        elif n > 0:
            record("seen-jobs job_keys=[]",
                   f"returns the whole cache ({n} keys) — seen_keys() is correct")
        else:
            record("seen-jobs job_keys=[]",
                   "returned 0 keys — either the cache is genuinely empty, or the "
                   "route filters by the keys given. See the follow-up below.")
    else:
        record("seen-jobs job_keys=[]", f"HTTP {status} — could not determine")

    # Distinguish "empty cache" from "route needs explicit keys": write one key
    # then ask again both ways.
    if do_writes and status == 200 and isinstance(empty_seen, list) and not empty_seen:
        print("\n  The cache looked empty. Writing one marked key, then asking twice,")
        print("  to tell 'genuinely empty' apart from 'this route needs the keys'.")

        st_w, bd_w, el_w = call(base, key, "seen-jobs",
                                {"user_id": user_id, "mode": "write",
                                 "job_keys": [MARKER]})
        show("seen-jobs", {"user_id": user_id, "mode": "write",
                           "job_keys": [MARKER]}, st_w, bd_w, el_w)

        st_a, bd_a, el_a = call(base, key, "seen-jobs",
                                {"user_id": user_id, "mode": "read",
                                 "job_keys": [MARKER]})
        show("seen-jobs", {"user_id": user_id, "mode": "read",
                           "job_keys": [MARKER]}, st_a, bd_a, el_a)
        explicit = (bd_a or {}).get("seen") if isinstance(bd_a, dict) else None

        st_b, bd_b, el_b = call(base, key, "seen-jobs",
                                {"user_id": user_id, "mode": "read", "job_keys": []})
        show("seen-jobs", {"user_id": user_id, "mode": "read", "job_keys": []},
             st_b, bd_b, el_b)
        blank = (bd_b or {}).get("seen") if isinstance(bd_b, dict) else None

        if explicit and MARKER in explicit and not blank:
            record("seen-jobs semantics",
                   "FILTERS by the keys given: job_keys=[] returns nothing. "
                   "seen_keys() MUST NOT rely on it.")
        elif blank and MARKER in blank:
            record("seen-jobs semantics",
                   "job_keys=[] returns the whole cache. seen_keys() is correct.")
        else:
            record("seen-jobs semantics",
                   f"inconclusive: explicit={explicit!r}, blank={blank!r}")

    # =======================================================================
    section("QUESTION 4 — is a 404 an unknown user, or a missing route?")

    st_u, bd_u, el_u = call(base, key, "get-profile",
                            {"user_id": "00000000-0000-0000-0000-000000000000"})
    show("get-profile", {"user_id": "0000...0000"}, st_u, bd_u, el_u)
    st_r, bd_r, el_r = call(base, key, "this-route-does-not-exist",
                            {"user_id": user_id})
    show("this-route-does-not-exist", {"user_id": user_id}, st_r, bd_r, el_r)
    record("404 distinction",
           f"unknown user -> HTTP {st_u}; unknown route -> HTTP {st_r}"
           + ("  (indistinguishable by status alone)" if st_u == st_r else ""))

    st_k, bd_k, el_k = call(base, "wrong-key-on-purpose", "get-profile",
                            {"user_id": user_id})
    show("get-profile (wrong key)", {"user_id": user_id}, st_k, bd_k, el_k)
    record("bad key", f"HTTP {st_k}")

    # =======================================================================
    section("QUESTION 2 — get-profile field names")

    st_p, bd_p, el_p = call(base, key, "get-profile", {"user_id": user_id})
    show("get-profile", {"user_id": user_id}, st_p, bd_p, el_p)
    if st_p == 200 and isinstance(bd_p, dict):
        profile = bd_p.get("profile") or {}
        record("get-profile", f"HTTP 200, profile keys: {sorted(profile)}")
    else:
        record("get-profile", f"HTTP {st_p}")

    if not do_writes:
        summarise()
        return 0

    # =======================================================================
    section("QUESTION 2 — update-run field names")

    run_payload = {
        "run_id": MARKER, "user_id": user_id, "profile_id": "probe",
        "status": "RUNNING", "cost_decision": "", "cost_estimate": {"usd_total": 0},
        "cost_fingerprint": "probe", "counts": {"probe": 1},
        "started_at": datetime.now().isoformat(), "finished_at": None,
    }
    st, bd, el = call(base, key, "update-run", run_payload)
    show("update-run", run_payload, st, bd, el)
    record("update-run", f"HTTP {st}" + (
        f", returned keys: {sorted((bd.get('run') or {}))}"
        if st == 200 and isinstance(bd, dict) else ""))

    # Does it read back on a second call? That would answer whether get_run
    # could be faked through it.
    st2, bd2, el2 = call(base, key, "update-run",
                         {"run_id": MARKER, "user_id": user_id})
    show("update-run (ids only)", {"run_id": MARKER, "user_id": user_id},
         st2, bd2, el2)
    if st2 == 200 and isinstance(bd2, dict):
        returned = bd2.get("run") or {}
        kept = returned.get("status")
        record("update-run as a read",
               f"ids-only call returned status={kept!r}"
               + (" — previous values preserved, could substitute for get-run"
                  if kept == "RUNNING"
                  else " — WIPED the other fields, must not be used as a read"))

    # =======================================================================
    section("QUESTION 2 + 3 — save-job-results field names and return shape")

    row = {
        "url": PROBE_URL, "title": "JobScout probe", "company": "Probe",
        "location": "Nowhere", "source": "probe", "published": "2026-09-15",
        "job_key": MARKER, "run_id": MARKER, "score": 5.0, "base_score": 5.0,
        "verdict": "MAYBE", "breakdown": {"probe": 5}, "match_signals": ["probe"],
        "gaps": [], "red_flags": [], "flags": {"probe": True},
        "extra": {"probe": "yes"}, "generated_text": "",
        "one_liner": "Connectivity probe, safe to delete.",
        "salary_range_market": "", "evaluated_at": datetime.now().isoformat(),
    }
    st, bd, el = call(base, key, "save-job-results",
                      {"user_id": user_id, "results": [row]})
    show("save-job-results", {"user_id": user_id, "results": [row]},
         st, bd, el, hide=("results",))
    if st == 200 and isinstance(bd, dict):
        record("save-job-results", f"HTTP 200, keys: {sorted(bd)}, "
                                   f"inserted={bd.get('inserted')!r} "
                                   f"({type(bd.get('inserted')).__name__})")
    else:
        record("save-job-results", f"HTTP {st} — {str(bd)[:160]}")

    # Same URL twice: does inserted drop to 0 on the upsert?
    st_d, bd_d, el_d = call(base, key, "save-job-results",
                            {"user_id": user_id, "results": [row]})
    show("save-job-results (same url again)", {"user_id": user_id, "results": [row]},
         st_d, bd_d, el_d, hide=("results",))
    if st_d == 200 and isinstance(bd_d, dict):
        record("upsert on (user_id, url)",
               f"second identical save reported inserted={bd_d.get('inserted')!r}")

    # =======================================================================
    section("QUESTION 5 — is 30s enough for a batch of 50?")

    batch = []
    for i in range(50):
        item = dict(row)
        item["url"] = f"{PROBE_URL}-batch-{i}"
        item["job_key"] = f"{MARKER}-batch-{i}"
        # A realistic row carries a full cover letter.
        item["generated_text"] = "x" * 3500
        batch.append(item)
    st, bd, el = call(base, key, "save-job-results",
                      {"user_id": user_id, "results": batch}, timeout=60.0)
    show("save-job-results (50 rows, ~180 kB)",
         {"user_id": user_id, "results": batch}, st, bd, el, hide=("results",))
    record("50-row batch",
           f"HTTP {st} in {el:.1f}s"
           + ("  — 30s default is fine" if st == 200 and el < 20
              else "  — RAISE the 30s timeout" if st == 200
              else ""))

    # =======================================================================
    section("QUESTION 2 — save-raw-dump field names")

    dump_payload = {
        "user_id": user_id, "connector": "jobscout-probe",
        "actor_id": "probe/actor", "apify_run_id": MARKER,
        "items_count": 1, "storage_path": f"probe/{STAMP}.json", "run_id": MARKER,
    }
    st, bd, el = call(base, key, "save-raw-dump", dump_payload)
    show("save-raw-dump", dump_payload, st, bd, el)
    record("save-raw-dump", f"HTTP {st}" + (
        f", returned keys: {sorted((bd.get('raw_dump') or {}))}"
        if st == 200 and isinstance(bd, dict) else ""))

    summarise()
    print(f"""
{'=' * 78}
ROWS CREATED — delete these when you are done
{'=' * 78}
  runs           run_id = {MARKER}
  job_results    url LIKE '{PROBE_URL}%'   (1 + 50 rows)
  seen_jobs      job_key LIKE '{MARKER}%'
  raw_dumps      connector = 'jobscout-probe'
""")
    return 0


def summarise() -> None:
    print(f"\n{'=' * 78}\nFINDINGS\n{'=' * 78}")
    for label, verdict in findings:
        print(f"  {label:26} {verdict}")


if __name__ == "__main__":
    raise SystemExit(main())
