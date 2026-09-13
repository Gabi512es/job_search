"""InfoJobs connector (Apify, paid) — declares its cost before spending it.

Ports `scrape_infojobs_apify` from the Camila notebook. Two things are gone:

- the blocking `input()` prompt (the cost decision now belongs to the pipeline
  and its guard, so this works from a server);
- the module-level dump path (the store is passed in).

The actor accepts no query/location fields: it takes an array of already-built
infojobs.net search URLs under `searchUrls`. One batched run covers every
keyword, which is why plan() declares exactly one actor start.
"""

from __future__ import annotations

from urllib.parse import urlencode

from jobscout.connectors.base import DumpStore, Secrets
from jobscout.filters import keyword_filter
from jobscout.jobs import JobPosting, strip_tracking
from jobscout.profile import InfoJobsApifySource

from cost_guard import ConnectorPlan, plan_infojobs

ACTOR_ID = "easyapi/infojobs-job-scraper"

# Verified manually on infojobs.net. provinceIds=9 applies the "Barcelona"
# filter (the page shows it as applied).
PROVINCE_IDS = {"Barcelona": 9}


def build_search_url(keyword: str, province: str) -> str:
    params: dict[str, object] = {"keyword": keyword}
    province_id = PROVINCE_IDS.get(province)
    if province_id:
        params["provinceIds"] = province_id
    return (
        "https://www.infojobs.net/jobsearch/search-results/list.xhtml?"
        + urlencode(params)
    )


def map_items(items: list[dict], filter_keywords: list[str]) -> list[JobPosting]:
    """Map raw dataset rows to JobPosting, dedupe by URL, then keyword-filter.

    Shared by the live path and the dump-replay path — a mapping fix applies to
    both, which is the whole point of keeping the raw dump.

    The useful fields are nested under item["offer"], not at the root.
    """
    jobs: list[JobPosting] = []
    seen_urls: set[str] = set()

    for item in items:
        offer = item.get("offer") or {}
        url = offer.get("link") or offer.get("url") or ""
        if url.startswith("//"):
            url = "https:" + url
        # Dedupe on the tracking-stripped URL: the same offer id reached from
        # two result pages differs only by `page=` and `sortBy=`.
        identity = strip_tracking(url)
        if not url or identity in seen_urls:
            continue
        seen_urls.add(identity)
        if not offer.get("title"):
            continue

        jobs.append(JobPosting(
            title=offer.get("title", ""),
            company=offer.get("companyName", ""),
            url=url,
            summary=offer.get("description", ""),
            source="infojobs",
            published=offer.get("publishedAt", ""),
            location=offer.get("city", ""),
            raw=offer,
        ))

    kept, dropped = keyword_filter(jobs, filter_keywords, fields=["title", "summary"])
    print(f"  [infojobs] {len(jobs)} mapped, {dropped} off-topic, {len(kept)} kept")
    return kept


class InfoJobsApifyConnector:
    type = "infojobs_apify"

    def __init__(self, profile_id: str, dumps: DumpStore | None = None):
        self.profile_id = profile_id
        self.dumps = dumps or DumpStore()

    def plan(self, cfg: InfoJobsApifySource) -> list[ConnectorPlan]:
        # Replaying a saved dump starts no actor and returns no new results, so
        # it costs nothing and bypasses the guard entirely.
        if cfg.reuse_dump:
            return []
        return [plan_infojobs(
            n_keywords=len(cfg.keywords),
            max_per_search=cfg.max_per_search,
        )]

    def fetch(self, cfg: InfoJobsApifySource, secrets: Secrets) -> list[JobPosting]:
        if cfg.reuse_dump:
            return self._fetch_from_dump(cfg)
        return self._fetch_live(cfg, secrets)

    # -- replay -------------------------------------------------------------

    def _fetch_from_dump(self, cfg: InfoJobsApifySource) -> list[JobPosting]:
        items, meta = self.dumps.load(self.type, self.profile_id)
        print(
            f"  [infojobs] replaying dump: {len(items)} items "
            f"(saved {meta.get('saved_at', '?')}, run {meta.get('apify_run_id', '?')}) "
            f"- 0 Apify calls, $0.00"
        )
        return map_items(items, cfg.filter_keywords)

    # -- live ---------------------------------------------------------------

    def _fetch_live(
        self, cfg: InfoJobsApifySource, secrets: Secrets
    ) -> list[JobPosting]:
        if not secrets.apify_token:
            print("  [infojobs] no APIFY_TOKEN, skipped")
            return []

        from apify_client import ApifyClient

        search_urls = [build_search_url(k, cfg.province) for k in cfg.keywords]
        max_items = cfg.max_per_search * len(search_urls)

        client = ApifyClient(secrets.apify_token)
        print(f"  [infojobs] {len(search_urls)} searches in one batched run")
        try:
            run = client.actor(ACTOR_ID).call(
                run_input={"searchUrls": search_urls, "maxItems": max_items}
            )
        except Exception as exc:
            print(f"  [infojobs] Apify error: {type(exc).__name__}: {exc}")
            return []

        items = list(client.dataset(run["defaultDatasetId"]).iterate_items())
        print(f"  [infojobs] {len(items)} items received (maxItems={max_items})")
        if len(items) > max_items:
            print(
                f"  [infojobs] WARNING: {len(items)} > {max_items} requested - "
                f"the actor did not honour maxItems, and you were billed for the "
                f"difference. This is why the cost guard prices the measured "
                f"per-URL count, not the requested cap."
            )

        # Save before mapping or filtering, so a mapping fix can be replayed
        # for free.
        path = self.dumps.save(
            self.type, self.profile_id, items,
            meta={
                "apify_run_id": run.get("id"),
                "dataset_id": run.get("defaultDatasetId"),
                "actor_id": ACTOR_ID,
                "search_urls": search_urls,
                "max_items_requested": max_items,
            },
        )
        print(f"  [infojobs] raw dump saved to {path}")

        return map_items(items, cfg.filter_keywords)
