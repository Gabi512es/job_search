"""RSS connector — free, no cost guard involvement.

Generalises `parse_feeds` from the Gabriel notebook. Two behaviours are
deliberately improved; both are asserted in tests/test_connectors.py:

1. `location` is now populated. The feeds carry it under different keys
   (`job_listing_location` on Jobicy, `region` on WeWorkRemotely); the notebook
   read none of them, so every RSS job reached the scorer with no location at
   all and the location dimension had nothing to work with.

2. Company extraction handles the "Company: Title" convention. The notebook
   only split on " at ", which WeWorkRemotely never uses, so company was empty
   for the largest live feed. An empty company also weakens deduplication,
   which groups on (company, title).
"""

from __future__ import annotations

import re

import feedparser

from jobscout.connectors.base import BROWSER_HEADERS, Secrets
from jobscout.jobs import JobPosting
from jobscout.profile import RssSource

from cost_guard import ConnectorPlan

# Location field names seen in the live feeds, most specific first.
LOCATION_KEYS = ("job_listing_location", "location", "region", "country", "state")
# Company field names some feeds provide directly.
COMPANY_KEYS = ("job_listing_company", "company")

# Words that disqualify a pre-colon prefix from being a company name: verbs and
# pronouns belong to a sentence, not to a company.
NON_COMPANY_WORDS = {
    "we", "i", "you", "our", "us", "is", "are", "am", "be",
    "hiring", "hire", "looking", "seeking", "join", "wanted", "now",
    "new", "urgent", "apply", "open", "opening", "job", "jobs", "role",
}


def _strip_html(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", value or "")).strip()


def split_company(title: str) -> tuple[str, str]:
    """Split a feed title into (title, company).

    Two conventions exist in the live feeds:
        "Acme Corp: Senior Engineer"   -> WeWorkRemotely
        "Senior Engineer at Acme Corp" -> generic

    Returns the title unchanged and an empty company when neither applies
    (hnrss titles are free-form sentences).
    """
    if " at " in title.lower():
        parts = re.split(r"\s+at\s+", title)
        if len(parts) >= 2:
            return " at ".join(parts[:-1]).strip(), parts[-1].strip()

    # "Company: Title" — the prefix must look like a company name rather than a
    # sentence fragment, or "We are hiring: ..." would yield a company called
    # "We are hiring". A company name is short and contains no verb or
    # first-person pronoun.
    if ":" in title:
        head, _, tail = title.partition(":")
        head, tail = head.strip(), tail.strip()
        words = {w.strip(",.").lower() for w in head.split()}
        if (
            head
            and tail
            and len(head) <= 60
            and len(head.split()) <= 4
            and not (words & NON_COMPANY_WORDS)
        ):
            return tail, head

    return title.strip(), ""


def _first_present(entry, keys) -> str:
    for key in keys:
        value = entry.get(key)
        if value:
            return str(value).strip()
    return ""


class RssConnector:
    type = "rss"

    def plan(self, cfg: RssSource) -> list[ConnectorPlan]:
        """Free source: nothing to price."""
        return []

    def fetch(self, cfg: RssSource, secrets: Secrets) -> list[JobPosting]:
        jobs: list[JobPosting] = []
        for feed_url in cfg.feeds:
            jobs.extend(self._fetch_one(feed_url))
        return jobs

    def _fetch_one(self, feed_url: str) -> list[JobPosting]:
        source = feed_url.split("/")[2].replace("www.", "")
        try:
            feed = feedparser.parse(feed_url, request_headers=BROWSER_HEADERS)
        except Exception as exc:
            print(f"  [rss] {source}: {type(exc).__name__}: {exc}")
            return []

        status = getattr(feed, "status", None)
        if status and status >= 400:
            print(f"  [rss] {source}: HTTP {status}, skipped")
            return []
        if not feed.entries:
            print(f"  [rss] {source}: HTTP {status}, 0 entries")
            return []

        out = []
        for entry in feed.entries:
            url = entry.get("link", "")
            if not url:
                continue
            raw_title = entry.get("title", "")
            title, company = split_company(raw_title)
            if not company:
                company = _first_present(entry, COMPANY_KEYS)

            out.append(JobPosting(
                title=title,
                company=company,
                url=url,
                summary=_strip_html(entry.get("summary", "")),
                source=source,
                published=entry.get("published", ""),
                location=_first_present(entry, LOCATION_KEYS),
                raw={"feed": feed_url, "title": raw_title},
            ))

        print(f"  [rss] {source}: HTTP {status}, {len(out)} jobs")
        return out
