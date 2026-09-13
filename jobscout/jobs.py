"""The common data contract every source connector produces.

All four connectors in the original notebooks already converged on this shape
without it being written down anywhere. Formalising it is what lets a new
source be added as one file with two methods (ARCHITECTURE.md § 3).

This module also owns job *identity*: normalising text, stripping tracking
parameters off a URL, and deriving the stable `job_key` used for the seen-jobs
cache. Identity primitives live next to the model because `job_key` is part of
what a JobPosting *is*.
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from pydantic import BaseModel, Field

# Query parameters that identify a click, not a job. Taken verbatim from the
# original `_TRACKING_PARAMS` in job_scout.ipynb.
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term",
    "trackingid", "refid", "trk", "trkinfo", "src", "ref", "referrer",
    "sessionid", "rsid", "f_tpr", "position", "originalsubdomain",
    # Added after observing real InfoJobs data: the same offer id reached from
    # result page 2 and page 3 produced two different URLs, so it survived URL
    # deduplication and got a different job_key on every run. These describe
    # how the offer was found, not which offer it is.
    "page", "sortby", "applicationorigin",
}


def normalize_text(value: str) -> str:
    """Lowercase, replace every non-word run with a single space, trim.

    This is the Camila notebook's `_norm`. The Gabriel notebook used
    `re.sub(r'[^\\w\\s]', '', ...)`, which DELETES punctuation instead of
    replacing it, so "Senior/Lead" collapsed to "seniorlead" and stopped
    matching "Senior Lead". Replacing is the safer of the two, so it wins.
    """
    return re.sub(r"\W+", " ", (value or "").lower()).strip()


def strip_tracking(url: str) -> str:
    """Drop tracking query parameters so the same job at two URLs compares equal."""
    try:
        parsed = urlparse(url)
        kept = {
            k: v
            for k, v in parse_qs(parsed.query, keep_blank_values=True).items()
            if k.lower() not in TRACKING_PARAMS
        }
        return urlunparse(parsed._replace(query=urlencode(kept, doseq=True)))
    except Exception:
        # A malformed URL is still a usable identity component as-is.
        return url


def job_key(title: str, company: str, url: str) -> str:
    """Stable per-job identity, used as the seen-jobs cache key.

    Unifies two divergent schemes (ARCHITECTURE.md § 5.2):
      - Gabriel used md5(url)                     -> broke when a URL gained a param
      - Camila used md5(title + company + url)    -> broke on tracking params

    Combining normalised title + company with a tracking-stripped URL survives
    both. Note this changes the key for existing caches - see the migration
    note in tests/test_filters.py.
    """
    basis = f"{normalize_text(title)}|{normalize_text(company)}|{strip_tracking(url)}"
    return hashlib.md5(basis.encode("utf-8")).hexdigest()[:12]


class JobPosting(BaseModel):
    """One job offer, whatever source it came from."""

    title: str = ""
    company: str = ""
    url: str = ""
    summary: str = ""
    source: str = ""
    published: str = ""
    # Generalised: only Xarxanet and InfoJobs filled this before. Populating it
    # everywhere is what lets location_not_in rules and geographic penalties
    # work for every profile.
    location: str = ""
    # The connector's original payload, kept so a mapping can be replayed
    # without re-scraping (and re-paying).
    raw: dict = Field(default_factory=dict)

    @property
    def key(self) -> str:
        return job_key(self.title, self.company, self.url)

    def text_for(self, fields: list[str]) -> str:
        """Lowercased concatenation of the named fields, for keyword matching.

        Unknown field names contribute nothing rather than raising: a profile
        naming a field that does not exist should not crash a run.
        """
        return " ".join(str(getattr(self, f, "") or "") for f in fields).lower()
