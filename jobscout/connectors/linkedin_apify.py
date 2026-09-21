"""LinkedIn connector (Apify, paid) — the one that previously had no cost guard.

Ports `scrape_linkedin_apify` from the Gabriel notebook. The behaviour is
unchanged, but it now declares its cost first, saves a raw dump, and fills
`location`.

Cost shape, and why it matters: this actor is called once per
(keyword x work_type) pair with no batching, so the notebook's six keywords and
two work types meant **twelve** actor starts of up to 100 results each per run,
entirely unmetered. plan() now declares all twelve.

Unknown: whether this actor honours its `count` parameter. It has never been
measured, unlike InfoJobs (where 108 requested returned 432). cost_guard emits
an explicit warning for that, and the estimate is a lower bound.
"""

from __future__ import annotations

from jobscout.connectors.base import ApifyResultError, DumpStore, dataset_id, Secrets
from jobscout.jobs import JobPosting, strip_tracking
from jobscout.profile import LinkedInApifySource

from cost_guard import ConnectorPlan, plan_linkedin

ACTOR_ID = "curious_coder/linkedin-jobs-scraper"

# LinkedIn's f_WT query parameter.
WORK_TYPE_CODES = {"onsite": 1, "remote": 2, "hybrid": 3}


def build_search_url(
    keyword: str, location: str, work_type: str, posted_within_days: int
) -> str:
    """Rebuild the notebook's LinkedIn search URL, parameter for parameter."""
    return (
        "https://www.linkedin.com/jobs/search/"
        f"?keywords={keyword.replace(' ', '+')}"
        f"&location={location}"
        f"&f_WT={WORK_TYPE_CODES.get(work_type, 3)}"
        f"&f_TPR=r{posted_within_days * 86400}"
        "&sortBy=DD"
    )


def map_items(items: list[dict]) -> list[JobPosting]:
    """Map raw dataset rows to JobPosting, deduping by URL.

    Field names vary between this actor's versions, hence the fallback chains -
    they are the notebook's, plus `location` which the notebook never read.
    """
    jobs: list[JobPosting] = []
    seen_urls: set[str] = set()

    for item in items:
        url = (
            item.get("jobUrl")
            or item.get("url")
            or item.get("link")
            or item.get("applyUrl", "")
        )
        identity = strip_tracking(url)
        if not url or identity in seen_urls:
            continue
        seen_urls.add(identity)

        jobs.append(JobPosting(
            title=item.get("title") or item.get("positionName", ""),
            company=item.get("companyName") or item.get("company", ""),
            url=url,
            summary=(item.get("description") or item.get("descriptionText", ""))[:2000],
            source="linkedin",
            published=item.get("postedAt") or item.get("publishedAt", ""),
            location=(
                item.get("location")
                or item.get("formattedLocation")
                or item.get("jobLocation", "")
            ),
            raw=item,
        ))

    return jobs


class LinkedInApifyConnector:
    type = "linkedin_apify"

    def __init__(self, profile_id: str, dumps: DumpStore | None = None):
        self.profile_id = profile_id
        self.dumps = dumps or DumpStore()

    def search_urls(self, cfg: LinkedInApifySource) -> list[str]:
        """Every search URL this connector would request. No network call."""
        return [
            build_search_url(keyword, cfg.location, work_type, cfg.posted_within_days)
            for keyword in cfg.keywords
            for work_type in cfg.work_types
        ]

    def plan(self, cfg: LinkedInApifySource) -> list[ConnectorPlan]:
        if cfg.reuse_dump:
            return []
        return [plan_linkedin(
            n_keywords=len(cfg.keywords),
            n_work_types=len(cfg.work_types),
            max_per_search=cfg.max_per_search,
        )]

    def fetch(self, cfg: LinkedInApifySource, secrets: Secrets) -> list[JobPosting]:
        if cfg.reuse_dump:
            return self._fetch_from_dump()
        return self._fetch_live(cfg, secrets)

    # -- replay -------------------------------------------------------------

    def _fetch_from_dump(self) -> list[JobPosting]:
        items, meta = self.dumps.load(self.type, self.profile_id)
        print(
            f"  [linkedin] replaying dump: {len(items)} items "
            f"(saved {meta.get('saved_at', '?')}) - 0 Apify calls, $0.00"
        )
        return map_items(items)

    # -- live ---------------------------------------------------------------

    def _fetch_live(
        self, cfg: LinkedInApifySource, secrets: Secrets
    ) -> list[JobPosting]:
        if not secrets.apify_token:
            print("  [linkedin] no APIFY_TOKEN, skipped")
            return []

        from apify_client import ApifyClient

        client = ApifyClient(secrets.apify_token)
        urls = self.search_urls(cfg)
        all_items: list[dict] = []
        failures: list[str] = []

        # One actor start per search URL - this actor takes a single-element
        # `urls` array, so there is no batching to be had.
        for i, url in enumerate(urls, 1):
            print(f"  [linkedin] start {i}/{len(urls)}: {url[:90]}")
            try:
                run = client.actor(ACTOR_ID).call(run_input={
                    "urls": [url],
                    "count": cfg.max_per_search,
                    "scrapeCompanyDetails": False,
                })
                items = list(client.dataset(dataset_id(run)).iterate_items())
                for item in items:
                    item.setdefault("_searchUrl", url)
                all_items.extend(items)
                print(f"  [linkedin]   -> {len(items)} items")
            except Exception as exc:
                failures.append(f"{i}/{len(urls)}: {type(exc).__name__}: {exc}")
                print(f"  [linkedin]   Apify error: {type(exc).__name__}: {exc}")

        # Saved BEFORE anything is raised, so whatever was paid for and did
        # arrive can still be replayed with reuse_dump instead of re-scraped.
        if all_items:
            path = self.dumps.save(
                self.type, self.profile_id, all_items,
                meta={"actor_id": ACTOR_ID, "search_urls": urls,
                      "max_items_requested": cfg.max_per_search * len(urls),
                      "failed_searches": failures},
            )
            print(f"  [linkedin] raw dump saved to {path}")

        # A retrieval failure is not "LinkedIn had nothing this week". The
        # actor started, so it billed; returning [] here made a paid outage
        # look exactly like an empty result set, and a run once cost $2.40 and
        # reported zero postings without anything appearing to go wrong.
        if failures:
            raise ApifyResultError(
                f"{len(failures)} of {len(urls)} LinkedIn searches were "
                f"started - and therefore billed - but their results could "
                f"not be read. The data is in the Apify console. Failures: "
                + " | ".join(failures[:4])
                + (f" | ... and {len(failures) - 4} more" if len(failures) > 4 else "")
            )

        jobs = map_items(all_items)
        print(f"  [linkedin] {len(all_items)} items, {len(jobs)} jobs after URL dedupe")
        return jobs
