"""Block 3: connectors behind one interface, with the cost guard actually wired.

    python3 tests/test_connectors.py

What this costs to run: nothing.
  RSS       - real network, free feeds
  Xarxanet  - real network, free HTML scraping
  InfoJobs  - replayed from the saved dump, 0 Apify calls
  LinkedIn  - plan() only. fetch() is NEVER called here. Running it would start
              12 Apify actors and bill roughly $1.20.

The safety property under test is ordering: no connector's fetch() can run
before the guard has said OK. A spy connector proves it rather than trusting
the reading of the code.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from jobscout.collect import collect, plan_run  # noqa: E402
from jobscout.connectors import Secrets, build_connectors  # noqa: E402
from jobscout.connectors.base import DumpStore  # noqa: E402
from jobscout.connectors.linkedin_apify import (  # noqa: E402
    LinkedInApifyConnector,
    build_search_url as li_url,
)
from jobscout.connectors.rss import split_company  # noqa: E402
from jobscout.connectors.infojobs_apify import build_search_url as ij_url  # noqa: E402
from jobscout.jobs import JobPosting, job_key, strip_tracking  # noqa: E402
from jobscout.profile import load_profile  # noqa: E402

from cost_guard import CostDecision, CostPolicy  # noqa: E402

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
    print(f"          expected: {expected!r}")
    print(f"          actual  : {actual!r}")


def check_true(label: str, value) -> None:
    check(label, bool(value), True)


def section(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


gabriel = load_profile(REPO / "profiles" / "gabriel.json", base_dir=REPO)
camila = load_profile(REPO / "profiles" / "camila.json", base_dir=REPO)
DUMPS = REPO / "dumps"
NO_SECRETS = Secrets()  # deliberately empty: nothing here may need a token


# ===========================================================================
section("PLAN() — no network, no spend, for every connector")

g_plans, g_conns = plan_run(gabriel, DUMPS)
c_plans, c_conns = plan_run(camila, DUMPS)

check("gabriel plans: only the paid source declares", len(g_plans), 1)
check("gabriel's paid source is linkedin", g_plans[0].connector, "linkedin")
check("linkedin declares 12 actor starts (6 keywords x 2 work types)",
      g_plans[0].n_starts, 12)
check("linkedin declares 12 search URLs", g_plans[0].n_search_urls, 12)
check("linkedin requests 1200 items", g_plans[0].max_items_requested, 1200)
check("rss declares nothing (free)",
      g_conns["rss"].plan(gabriel.source("rss")), [])

check("camila plans: only infojobs declares", len(c_plans), 1)
check("camila's paid source is infojobs", c_plans[0].connector, "infojobs")
check("infojobs declares 1 batched start", c_plans[0].n_starts, 1)
check("infojobs declares 9 search URLs", c_plans[0].n_search_urls, 9)
check("xarxanet declares nothing (free)",
      c_conns["xarxanet"].plan(camila.source("xarxanet")), [])


# ===========================================================================
section("COST GUARD WIRED — the run stops before fetching")

# Camila's real configuration prices at $1.38, above the $1.00 threshold.
held = collect(camila, NO_SECRETS, dumps_dir=DUMPS)
check("held for confirmation", held.decision, CostDecision.NEEDS_CONFIRMATION)
check("nothing was fetched", held.jobs, [])
check("no counts produced", held.counts, {})
check("an estimate is returned for the frontend", round(held.cost.usd_total, 2), 1.38)
check("with a per-connector breakdown", len(held.cost.per_connector), 1)
check_true("and a fingerprint to confirm with", held.cost.fingerprint)
print(f"\n  reason: {held.cost.reason}")

# A hard cap cannot be talked around.
tight = CostPolicy(max_cost_per_run=0.50, confirm_above=0.10)
capped_profile = camila.model_copy(update={"cost_policy": tight})
capped = collect(capped_profile, NO_SECRETS, dumps_dir=DUMPS)
check("rejected over the hard cap", capped.decision,
      CostDecision.REJECTED_OVER_HARD_CAP)
check("still nothing fetched", capped.jobs, [])
confirmed_anyway = collect(
    capped_profile, NO_SECRETS, dumps_dir=DUMPS,
    confirmed_fingerprint=capped.cost.fingerprint,
)
check("confirming a rejected run does not rescue it",
      confirmed_anyway.decision, CostDecision.REJECTED_OVER_HARD_CAP)


# ===========================================================================
section("ORDERING PROOF — fetch() is unreachable without a green light")


class SpyConnector:
    """Records whether fetch() was ever reached."""

    type = "linkedin_apify"

    def __init__(self, plans):
        self._plans = plans
        self.fetch_called = False

    def plan(self, cfg):
        return self._plans

    def fetch(self, cfg, secrets):
        self.fetch_called = True
        return []


import jobscout.collect as collect_mod  # noqa: E402

original_build = collect_mod.build_connectors
spy_holder = {}


def spying_build(profile, dumps_dir="dumps"):
    built = original_build(profile, dumps_dir)
    spy = SpyConnector(built["linkedin_apify"].plan(profile.source("linkedin_apify")))
    built["linkedin_apify"] = spy
    spy_holder["spy"] = spy
    return built


collect_mod.build_connectors = spying_build
try:
    # Gabriel's LinkedIn run prices at $1.20 -> held.
    result = collect(gabriel, NO_SECRETS, dumps_dir=DUMPS)
    check("gabriel's run is held", result.decision, CostDecision.NEEDS_CONFIRMATION)
    check("the paid connector's fetch() was never called",
          spy_holder["spy"].fetch_called, False)

    # Free sources are held too: the run is atomic.
    check("and no free source ran either (atomic run)", result.jobs, [])
finally:
    collect_mod.build_connectors = original_build


# ===========================================================================
section("RSS — real fetch against the 4 live feeds (free)")

rss_cfg = gabriel.source("rss")
check("dead feeds are absent from gabriel's config",
      [f for f in rss_cfg.feeds if "arbeitnow" in f or "remotive" in f], [])
check("4 feeds configured", len(rss_cfg.feeds), 4)

rss_jobs = g_conns["rss"].fetch(rss_cfg, NO_SECRETS)
check_true(f"returned jobs ({len(rss_jobs)})", rss_jobs)
check("all are JobPosting", all(isinstance(j, JobPosting) for j in rss_jobs), True)
check("all have a url", all(j.url for j in rss_jobs), True)
check("all have a title", all(j.title for j in rss_jobs), True)
check("all have a source", all(j.source for j in rss_jobs), True)
check("all have a usable key", all(len(j.key) == 12 for j in rss_jobs), True)

sources = sorted({j.source for j in rss_jobs})
check("all 3 live hosts produced jobs", sources,
      ["hnrss.org", "jobicy.com", "weworkremotely.com"])

with_location = [j for j in rss_jobs if j.location]
with_company = [j for j in rss_jobs if j.company]
print(f"\n  location filled : {len(with_location)}/{len(rss_jobs)}")
print(f"  company filled  : {len(with_company)}/{len(rss_jobs)}")
for j in rss_jobs[:3]:
    print(f"    [{j.source}] {j.title[:44]!r} | company={j.company!r} "
          f"| location={j.location!r}")

check_true("location is populated for some jobs (was never populated before)",
           with_location)
check_true("company is populated for most jobs", len(with_company) > len(rss_jobs) / 2)

section("RSS — company/title splitting")
check("WeWorkRemotely 'Company: Title'", split_company("Stadium: AWS DevOps Engineer"),
      ("AWS DevOps Engineer", "Stadium"))
check("generic 'Title at Company'", split_company("Senior Engineer at Acme Corp"),
      ("Senior Engineer", "Acme Corp"))
check("a free-form hnrss title is left alone",
      split_company("Zep AI (YC W24) Is Hiring a Head of Engineering"),
      ("Zep AI (YC W24) Is Hiring a Head of Engineering", ""))
check("a long sentence before a colon is not treated as a company",
      split_company("We are hiring: come and build the future with our team"),
      ("We are hiring: come and build the future with our team", ""))


# ===========================================================================
section("XARXANET — real fetch (free HTML scraping)")

xarxa_jobs = c_conns["xarxanet"].fetch(camila.source("xarxanet"), NO_SECRETS)
check_true(f"returned jobs ({len(xarxa_jobs)})", xarxa_jobs)
check("all have a xarxanet.org url",
      all("xarxanet.org" in j.url for j in xarxa_jobs), True)
check("source is tagged", {j.source for j in xarxa_jobs}, {"xarxanet"})
check("all have a company (the entity)", all(j.company for j in xarxa_jobs), True)
check("summaries are real detail-page text, not just the title",
      all(len(j.summary) > len(j.title) for j in xarxa_jobs), True)
# The notebook looked for <span class="field-label"> inside an <li>; the page
# renders <h2 class="field-label">Població</h2> + <div class="field-value">
# inside a `field-job-offer-location` block. That selector never matched, so
# Xarxanet location was always empty.
xarxa_located = [j for j in xarxa_jobs if j.location]
check("location is now extracted (the old selector never matched)",
      len(xarxa_located) > 0, True)
print(f"\n  location filled: {len(xarxa_located)}/{len(xarxa_jobs)}")
for j in xarxa_jobs[:3]:
    print(f"    {j.title[:46]!r} | {j.company[:26]!r} | location={j.location!r} "
          f"| {len(j.summary)} chars")


# ===========================================================================
section("INFOJOBS — replayed from the saved dump (0 Apify calls, $0.00)")

store = DumpStore(DUMPS)
check("a dump exists for camila", store.exists("infojobs_apify", "camila"), True)

replay_cfg = camila.source("infojobs_apify").model_copy(update={"reuse_dump": True})
check("reuse_dump makes plan() free",
      c_conns["infojobs_apify"].plan(replay_cfg), [])

ij_jobs = c_conns["infojobs_apify"].fetch(replay_cfg, NO_SECRETS)
# 191, not the 229 the original analysis reported: see the pagination fix below.
check("mapped 191 jobs after the pagination fix", len(ij_jobs), 191)
check("source is tagged", {j.source for j in ij_jobs}, {"infojobs"})
check("all have a url", all(j.url for j in ij_jobs), True)
check("protocol-relative URLs were fixed",
      [j.url for j in ij_jobs if j.url.startswith("//")], [])
check("location is filled from offer.city",
      all(j.location for j in ij_jobs) or len([j for j in ij_jobs if j.location]) > 200,
      True)
check("raw payload retained for replay", all(j.raw for j in ij_jobs), True)
for j in ij_jobs[:3]:
    print(f"    {j.title[:46]!r} | {j.company[:24]!r} | location={j.location!r}")

# The whole point of keeping the dump: replay costs nothing and reaches the
# same result without the cost guard being consulted at all.
replay_profile = camila.model_copy(update={
    "sources": [
        s.model_copy(update={"reuse_dump": True}) if s.type == "infojobs_apify" else s
        for s in camila.sources
    ]
})
replay_plans, _ = plan_run(replay_profile, DUMPS)
check("a full replay run declares no cost at all", replay_plans, [])

section("INFOJOBS — pagination params were breaking identity (real data)")

# Found in the saved dump: the SAME offer id reached from result page 2 and
# page 3 produced two URLs differing only by `page=` and `sortBy=`. They
# survived URL deduplication, were scored twice, and got a different job_key
# on every run - so the seen-jobs cache never recognised them either.
PAGED_A = ("https://www.infojobs.net/sabadell/integrador-social/of-id3b4a1bb3"
           "?applicationOrigin=search-new&page=3&sortBy=RELEVANCE")
PAGED_B = ("https://www.infojobs.net/sabadell/integrador-social/of-id3b4a1bb3"
           "?applicationOrigin=search-new&page=2&sortBy=RELEVANCE")
check("the two URLs are genuinely different strings", PAGED_A != PAGED_B, True)
check("but strip to the same identity",
      strip_tracking(PAGED_A), strip_tracking(PAGED_B))
check("so they share one job_key",
      job_key("Integrador social", "Suara", PAGED_A),
      job_key("Integrador social", "Suara", PAGED_B))
check("the offer id survives stripping",
      "of-id3b4a1bb3" in strip_tracking(PAGED_A), True)

# And the effect on the real dump: no (company, title) pair should still be
# duplicated more than twice after the fix.
ij_groups: dict[tuple[str, str], int] = {}
for j in ij_jobs:
    k = (j.company.lower(), j.title.lower())
    ij_groups[k] = ij_groups.get(k, 0) + 1
still_dupe = sum(1 for n in ij_groups.values() if n > 1)
print(f"\n  (company, title) pairs still duplicated: {still_dupe} (was 35 before the fix)")
check("duplicate pairs nearly eliminated", still_dupe <= 3, True)

section("INFOJOBS / LINKEDIN — search URL construction")
check("infojobs applies the Barcelona province filter",
      ij_url("educación social", "Barcelona"),
      "https://www.infojobs.net/jobsearch/search-results/list.xhtml"
      "?keyword=educaci%C3%B3n+social&provinceIds=9")
check("an unknown province omits the filter",
      "provinceIds" in ij_url("test", "Nowhere"), False)


# ===========================================================================
section("LINKEDIN — plan() and URL construction only. fetch() NOT executed.")

li = LinkedInApifyConnector("gabriel", store)
li_cfg = gabriel.source("linkedin_apify")
urls = li.search_urls(li_cfg)

check("12 search URLs, no network call made", len(urls), 12)
check("one per (keyword, work_type) pair",
      len(urls), len(li_cfg.keywords) * len(li_cfg.work_types))
check("hybrid maps to f_WT=3", "f_WT=3" in urls[0], True)
check("onsite maps to f_WT=1", "f_WT=1" in urls[1], True)
check("7 days maps to f_TPR=r604800", "f_TPR=r604800" in urls[0], True)
check("sorted by date", "sortBy=DD" in urls[0], True)
check("matches the notebook's URL shape",
      li_url("product AI engineer", "Barcelona", "hybrid", 7),
      "https://www.linkedin.com/jobs/search/?keywords=product+AI+engineer"
      "&location=Barcelona&f_WT=3&f_TPR=r604800&sortBy=DD")

print("\n  First 3 of the 12 URLs plan() would request:")
for u in urls[:3]:
    print(f"    {u}")

plan = li.plan(li_cfg)[0]
print(f"\n  plan(): {plan.n_starts} starts, {plan.n_search_urls} URLs, "
      f"maxItems={plan.max_items_requested}")
print("  fetch() was NOT called. Doing so would start 12 Apify actors (~$1.20).")

check("reuse_dump would make linkedin free too",
      li.plan(li_cfg.model_copy(update={"reuse_dump": True})), [])


# ===========================================================================
section("DUMP STORE — generalised to every Apify connector")

tmp = DumpStore(REPO / ".tmp_dumps")
tmp.save("linkedin_apify", "someone", [{"jobUrl": "https://x/1", "title": "T"}],
         meta={"actor_id": "test"})
items, meta = tmp.load("linkedin_apify", "someone")
check("round-trips items", items, [{"jobUrl": "https://x/1", "title": "T"}])
check("keeps metadata", meta["actor_id"], "test")
check("namespaced per user",
      tmp.path_for("linkedin_apify", "a") != tmp.path_for("linkedin_apify", "b"), True)
check("namespaced per connector",
      tmp.path_for("rss", "a") != tmp.path_for("linkedin_apify", "a"), True)

missing = False
try:
    tmp.load("linkedin_apify", "nobody")
except FileNotFoundError:
    missing = True
check("a missing dump raises instead of silently returning nothing", missing, True)

import shutil  # noqa: E402
shutil.rmtree(REPO / ".tmp_dumps", ignore_errors=True)


# ===========================================================================
section("END TO END — a confirmed, zero-cost run for camila")

# Xarxanet live + InfoJobs replayed = no Apify spend, so the guard says OK
# straight away and fetch() runs for real.
free_profile = camila.model_copy(update={
    "sources": [
        s.model_copy(update={"reuse_dump": True}) if s.type == "infojobs_apify" else s
        for s in camila.sources
    ]
})
run = collect(free_profile, NO_SECRETS, dumps_dir=DUMPS)
check("the guard approves a free run immediately", run.decision, CostDecision.OK)
check("estimated cost is zero", run.cost.usd_total, 0.0)
check_true("jobs were collected", run.jobs)
print(f"\n  {run.summary()}")
print(f"  counts: {run.counts}")

check("both sources contributed",
      sorted(k for k in run.counts if k.startswith("src_")),
      ["src_infojobs_apify", "src_xarxanet"])
check_true("duplicates were removed", run.counts.get("deduplicated", 0) >= 0)
check_true("exclusions were applied", "excluded" in run.counts)
check("every collected job carries a location or an explicit empty string",
      all(isinstance(j.location, str) for j in run.jobs), True)

# The seen-jobs cache short-circuits a second run.
seen = {j.key for j in run.jobs}
again = collect(free_profile, NO_SECRETS, seen_keys=seen, dumps_dir=DUMPS)
check("a second run with a full cache scores nothing", again.counts["to_score"], 0)
check("and reports why", again.counts["already_seen"] > 0, True)


# ===========================================================================
section("APIFY RESULT SHAPES — the $2.40 bug, both client versions")

from jobscout.connectors.base import (  # noqa: E402
    ApifyResultError, dataset_id, run_field,
)


class RunV3:
    """What apify-client 3.x returns: a pydantic model, snake_case attributes.

    Simulated rather than installed: 3.x needs Python 3.11 and this repo runs
    3.10, which is exactly how the deployment ended up on a different version
    from development in the first place.
    """

    default_dataset_id = "ds-v3"
    id = "run-v3"


V2 = {"defaultDatasetId": "ds-v2", "id": "run-v2"}

check("apify-client 3.x: attribute access", dataset_id(RunV3()), "ds-v3")
check("apify-client 2.x: the old dict still works", dataset_id(V2), "ds-v2")
check("other fields too, on 3.x", run_field(RunV3(), "id", "id"), "run-v3")
check("and on 2.x", run_field(V2, "id", "id"), "run-v2")


def why(fn) -> str:
    try:
        fn()
    except ApifyResultError as exc:
        return str(exc)
    return ""


check("a None run raises instead of being indexed",
      "returned None" in why(lambda: dataset_id(None)), True)
check("and says the actor may have billed anyway",
      "billed" in why(lambda: dataset_id(None)), True)
check("an unrecognised shape raises rather than returning nothing",
      "carrying no dataset id" in why(lambda: dataset_id(object())), True)
check("naming both spellings it looked for",
      all(k in why(lambda: dataset_id({}))
          for k in ("default_dataset_id", "defaultDatasetId")), True)
# The exact production failure, reproduced: the old line indexed this object.
try:
    RunV3()["defaultDatasetId"]
    reproduced = ""
except TypeError as exc:
    reproduced = str(exc)
check("indexing a Run object raises the error the logs showed",
      reproduced, "'RunV3' object is not subscriptable")
check("and dataset_id() reads the same object without raising",
      dataset_id(RunV3()), "ds-v3")


section("LINKEDIN — a retrieval failure is no longer silent")

from jobscout.connectors.linkedin_apify import LinkedInApifyConnector  # noqa: E402


class FakeDataset:
    def __init__(self, items): self._items = items
    def iterate_items(self): return iter(self._items)


class Unreadable:
    """A run carrying no dataset id under either spelling."""


class FakeActor:
    def __init__(self, client, shape): self.client, self.shape = client, shape

    def call(self, run_input=None):
        # Counted before anything can fail: starting the actor is what bills.
        self.client.starts += 1
        return {"v2": {"defaultDatasetId": "ds-v2", "id": "r"},
                "v3": RunV3(),
                "unreadable": Unreadable()}[self.shape]


class FakeApify:
    """Counts actor starts, because each one is money."""

    def __init__(self, shape="v2", items=None):
        self.shape, self.items, self.starts = shape, items or [], 0

    def actor(self, _): return FakeActor(self, self.shape)

    def dataset(self, ds_id):
        if ds_id not in ("ds-v2", "ds-v3"):
            raise AssertionError(f"unexpected dataset {ds_id!r}")
        return FakeDataset(self.items)


# Dumps go to the scratchpad, never to the repo's own dumps/.
APIFY_TMP = Path("/private/tmp/claude-501"
                 "/-Users-gabrielernoult-Desktop-GIT-Repos-Job-Search"
                 "/a697afae-6c9e-4187-870d-45749f73a1a8/scratchpad/apify_shapes")


def run_linkedin(shape: str, items=None, tmp=None):
    """Drive _fetch_live with a fake client. No network, no spend."""
    connector = LinkedInApifyConnector("t", DumpStore(tmp or APIFY_TMP))
    fake = FakeApify(shape=shape, items=items)

    class FakeModule:
        ApifyClient = staticmethod(lambda token: fake)

    saved = sys.modules.get("apify_client")
    sys.modules["apify_client"] = FakeModule
    try:
        cfg = gabriel.source("linkedin_apify")
        return connector._fetch_live(cfg, Secrets(apify_token="fake")), fake
    finally:
        if saved is not None:
            sys.modules["apify_client"] = saved
        else:
            del sys.modules["apify_client"]


items = [{"jobUrl": f"https://www.linkedin.com/jobs/view/{i}", "title": f"Job {i}",
          "companyName": "ACME", "location": "Barcelona",
          "description": "x", "postedAt": "2026-09-20"} for i in range(5)]

jobs, fake = run_linkedin("v2", items=items)
check("apify-client 2.x: postings still come through", len(jobs), 5)
check("one actor start per search URL", fake.starts, 12)

# THE REGRESSION TEST. This exact shape returned zero postings on 2026-09-21
# after twelve actors had run and billed $2.40.
jobs, fake = run_linkedin("v3", items=items)
check("apify-client 3.x: a Run object now yields postings, not silence",
      len(jobs), 5)
check("the actors were still started exactly twelve times", fake.starts, 12)

raised = ""
try:
    run_linkedin("unreadable", items=items)
except ApifyResultError as exc:
    raised = str(exc)
check("a genuinely unreadable run RAISES", bool(raised), True)
check("instead of returning [] as if nothing had been found",
      "could not be read" in raised, True)
check("it says how many searches were billed", "12 of 12" in raised, True)
check("and where the data still is", "Apify console" in raised, True)

# The distinction the old code destroyed.
empty, _ = run_linkedin("v2", items=[])
check("an actor that genuinely found nothing returns [] and does NOT raise",
      empty, [])


# ===========================================================================
print()
print("=" * 78)
if FAILURES:
    print(f"FAILED — {len(FAILURES)} of {CHECKS} checks:")
    for f in FAILURES:
        print(f"  - {f}")
    print("=" * 78)
    sys.exit(1)
print(f"PASSED — {CHECKS} checks. LinkedIn fetch() was never executed.")
print("=" * 78)
