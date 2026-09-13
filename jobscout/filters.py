"""Deduplication and profile-driven exclusion, applied before any scoring.

Everything here is pure: no network, no API key, no cost. Filtering hard before
scoring is what keeps the Haiku bill down, so these run first.
"""

from __future__ import annotations

from collections import OrderedDict

from jobscout.jobs import JobPosting, normalize_text
from jobscout.profile import ExclusionRule, KeywordPenalty


def deduplicate(jobs: list[JobPosting]) -> tuple[list[JobPosting], int]:
    """Collapse duplicates, keeping the copy with the longest summary.

    Grouped on (normalised company, normalised title) only - deliberately NOT
    on the URL. The same job cross-posted to LinkedIn and to a company board
    has two URLs and should still collapse to one, and we want the richer
    description of the two to be the one that gets scored.

    This is the Camila notebook's strategy. Gabriel's included the URL in the
    key, so cross-source duplicates survived and were scored (and billed)
    twice. Order of first appearance is preserved.

    Returns (kept, n_removed).
    """
    groups: OrderedDict[tuple[str, str], list[JobPosting]] = OrderedDict()
    for job in jobs:
        key = (normalize_text(job.company), normalize_text(job.title))
        groups.setdefault(key, []).append(job)

    kept = [
        group[0] if len(group) == 1 else max(group, key=lambda j: len(j.summary or ""))
        for group in groups.values()
    ]
    return kept, len(jobs) - len(kept)


def _rule_matches(job: JobPosting, rule: ExclusionRule) -> bool:
    """True if this rule says to drop the job."""
    haystack = job.text_for(rule.fields)

    if rule.kind == "any_keyword":
        return any(kw.lower() in haystack for kw in rule.keywords)

    if rule.kind == "keyword_unless":
        # Drop only if a trigger word appears AND no rescuing word does.
        # Camila's case: "voluntariado" is fine when "contrato" is also there.
        if not any(kw.lower() in haystack for kw in rule.keywords):
            return False
        return not any(kw.lower() in haystack for kw in rule.unless_keywords)

    if rule.kind == "location_not_in":
        # An empty location is never excluded: absence of data is not evidence
        # of a bad location, and dropping those would silently lose offers
        # from sources that do not report a location.
        if not haystack.strip():
            return False
        if any(term.lower() in haystack for term in rule.allow):
            return False
        return any(term.lower() in haystack for term in rule.deny)

    raise ValueError(f"unknown exclusion kind: {rule.kind!r}")


def exclusion_reason(job: JobPosting, rules: list[ExclusionRule]) -> str | None:
    """Return the kind of the first rule that excludes this job, else None.

    Returning the reason rather than a bare bool means the UI can later tell a
    user *why* an offer never appeared, and the tests can assert on it.
    """
    for rule in rules:
        if _rule_matches(job, rule):
            return rule.kind
    return None


def apply_exclusions(
    jobs: list[JobPosting], rules: list[ExclusionRule]
) -> tuple[list[JobPosting], dict[str, int]]:
    """Drop excluded jobs. Returns (kept, count per rule kind)."""
    kept: list[JobPosting] = []
    counts: dict[str, int] = {}
    for job in jobs:
        reason = exclusion_reason(job, rules)
        if reason is None:
            kept.append(job)
        else:
            counts[reason] = counts.get(reason, 0) + 1
    return kept, counts


def penalty_applies(job: JobPosting, penalty: KeywordPenalty) -> bool:
    """Whether a Python-side keyword penalty matches this job.

    Lives here rather than in scoring.py so that all keyword-against-job
    matching uses one implementation. Used by compute_verdict in block 4.
    """
    haystack = job.text_for(penalty.fields)
    return any(kw.lower() in haystack for kw in penalty.keywords)


def filter_new(
    jobs: list[JobPosting], seen_keys: set[str]
) -> tuple[list[JobPosting], int]:
    """Drop jobs already in the seen-jobs cache. Returns (new, n_skipped)."""
    fresh = [j for j in jobs if j.key not in seen_keys]
    return fresh, len(jobs) - len(fresh)


def keyword_filter(
    jobs: list[JobPosting], keywords: list[str], fields: list[str] | None = None
) -> tuple[list[JobPosting], int]:
    """Keep only jobs matching at least one keyword.

    This is the source-level relevance filter (Xarxanet's and InfoJobs'
    `filter_keywords`), applied by connectors before a job ever reaches the
    profile's exclusion rules. An empty keyword list keeps everything.
    """
    if not keywords:
        return jobs, 0
    fields = fields or ["title", "summary"]
    kept = [
        j for j in jobs
        if any(kw.lower() in j.text_for(fields) for kw in keywords)
    ]
    return kept, len(jobs) - len(kept)
