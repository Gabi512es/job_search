"""Xarxanet connector — free HTML scraping, no cost guard involvement.

Ports `scrape_xarxanet` from the Camila notebook unchanged in substance: a
listing page, then every detail page in parallel, then a keyword filter applied
in Python (the site has no URL-based search).

The CSS selectors are the site's, so they are the fragile part. They are
isolated in `_parse_listing` / `_parse_detail` and the connector degrades to an
empty list with a printed reason rather than raising, so one broken source
cannot take a whole run down.
"""

from __future__ import annotations

import concurrent.futures

import httpx
from bs4 import BeautifulSoup

from jobscout.connectors.base import Secrets
from jobscout.filters import keyword_filter
from jobscout.jobs import JobPosting
from jobscout.profile import XarxanetSource

from cost_guard import ConnectorPlan

LISTING_URL = "https://xarxanet.org/ofertes-feina"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ca,es;q=0.8",
}


class XarxanetConnector:
    type = "xarxanet"

    def __init__(self, timeout: int = 20, workers: int = 5):
        self.timeout = timeout
        self.workers = workers

    def plan(self, cfg: XarxanetSource) -> list[ConnectorPlan]:
        """Free source: nothing to price."""
        return []

    def fetch(self, cfg: XarxanetSource, secrets: Secrets) -> list[JobPosting]:
        stubs = self._fetch_listing()
        if not stubs:
            return []

        jobs = self._fetch_details(stubs)

        kept, dropped = keyword_filter(
            jobs, cfg.filter_keywords, fields=["title", "summary"]
        )
        print(f"  [xarxanet] {len(jobs)} listed, {dropped} off-topic, {len(kept)} kept")
        return kept

    # -- listing page -------------------------------------------------------

    def _fetch_listing(self) -> list[dict]:
        try:
            response = httpx.get(
                LISTING_URL, headers=HEADERS, timeout=self.timeout,
                follow_redirects=True,
            )
        except Exception as exc:
            print(f"  [xarxanet] listing error: {type(exc).__name__}: {exc}")
            return []
        if response.status_code != 200:
            print(f"  [xarxanet] listing HTTP {response.status_code}")
            return []
        return self._parse_listing(response.text)

    @staticmethod
    def _parse_listing(html: str) -> list[dict]:
        soup = BeautifulSoup(html, "html.parser")
        stubs = []
        for article in soup.find_all("article", class_="job-offer"):
            title_tag = article.find("h2", class_="job-offer__title")
            anchor = title_tag.find("a") if title_tag else None
            if not anchor:
                continue

            url = anchor.get("href", "")
            if url and not url.startswith("http"):
                url = "https://xarxanet.org" + url

            entity = article.find("li", class_="job-offer__entity")
            value = entity.find("span", class_="field-value") if entity else None

            time_tag = article.find("time")

            stubs.append({
                "title": anchor.get_text(strip=True),
                "company": value.get_text(strip=True) if value else "",
                "url": url,
                "published": time_tag.get("datetime", "") if time_tag else "",
            })
        return stubs

    # -- detail pages -------------------------------------------------------

    def _fetch_details(self, stubs: list[dict]) -> list[JobPosting]:
        jobs: list[JobPosting] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = [pool.submit(self._fetch_detail, s) for s in stubs]
            for future in concurrent.futures.as_completed(futures):
                job = future.result()
                if job:
                    jobs.append(job)
        # as_completed returns out of order; restore listing order so runs are
        # reproducible.
        order = {s["url"]: i for i, s in enumerate(stubs)}
        jobs.sort(key=lambda j: order.get(j.url, 10**6))
        return jobs

    def _fetch_detail(self, stub: dict) -> JobPosting | None:
        summary, location = stub["title"], ""
        try:
            response = httpx.get(
                stub["url"], headers=HEADERS, timeout=self.timeout,
                follow_redirects=True,
            )
            if response.status_code == 200:
                summary, location = self._parse_detail(response.text, stub["title"])
        except Exception as exc:
            print(f"  [xarxanet] detail error {stub['url']}: {type(exc).__name__}")

        return JobPosting(
            title=stub["title"],
            company=stub["company"],
            url=stub["url"],
            summary=summary,
            source="xarxanet",
            published=stub["published"],
            location=location,
            raw={"listing": stub},
        )

    @staticmethod
    def _parse_detail(html: str, fallback_title: str) -> tuple[str, str]:
        soup = BeautifulSoup(html, "html.parser")
        article = soup.find("article")
        summary = (
            article.get_text(separator=" ", strip=True) if article else fallback_title
        )
        return summary, XarxanetConnector._parse_location(soup)

    @staticmethod
    def _parse_location(soup: BeautifulSoup) -> str:
        """Read the "Població" field.

        The notebook looked for a <span class="field-label"> inside an <li>.
        The page actually renders an <h2 class="field-label">Població</h2> next
        to a <div class="field-value">, inside a block whose class contains
        `field-job-offer-location`, and outside any <li>. That selector
        therefore never matched, and every Xarxanet job reached the scorer with
        no location - which is why the Maresme penalty had to fall back on the
        title.

        Two strategies, most specific first, and neither assumes a tag name.
        """
        block = soup.find(
            class_=lambda c: c and "field-job-offer-location" in " ".join(
                c if isinstance(c, list) else [c]
            )
        )
        if block:
            value = block.find(class_="field-value")
            if value:
                return value.get_text(strip=True)

        # Fallback: any label reading "Població"/"Poblacio", then the nearest
        # following field-value.
        for label in soup.find_all(class_="field-label"):
            if "oblaci" in label.get_text().lower():
                value = label.find_next(class_="field-value")
                if value:
                    return value.get_text(strip=True)
        return ""
