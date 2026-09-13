"""Named derived values for export columns.

A column's `value` is one of:
    field:<name>     read straight off the JobResult
    computed:<name>  one of the functions registered here
    flag:<key>       a boolean from the profile's llm_flags, as yes/no

A plain name -> function registry, deliberately not an expression language:
profiles come from a database and will eventually come from a web form, so a
column definition must never be something that gets evaluated.
"""

from __future__ import annotations

from datetime import datetime

from jobscout.profile import UserProfile
from jobscout.store.base import Application, JobResult


def _days_old(published: str) -> int:
    """Age in days, or a large number when the date cannot be read."""
    if not published:
        return 999
    try:
        from email.utils import parsedate_to_datetime
        parsed = parsedate_to_datetime(str(published))
        return (datetime.now(parsed.tzinfo) - parsed).days
    except Exception:
        pass
    try:
        return (datetime.now() - datetime.fromisoformat(str(published)[:10])).days
    except Exception:
        return 999


def priority_key(score: float, published: str) -> str:
    """high / medium / low, from the notebook's own thresholds.

    Urgency is either quality or freshness: a very strong match, or a posting
    published in the last day, both deserve attention first.
    """
    score = float(score or 0)
    days = _days_old(published)
    if score >= 8.5 or days <= 1:
        return "high"
    if score >= 7.5 or days <= 3:
        return "medium"
    return "low"


def target_salary(profile: UserProfile, result: JobResult) -> str:
    """The candidate's own salary expectation for this role's seniority.

    Narrow by design (ARCHITECTURE.md 2.3): profiles without the rule get "".
    """
    rule = profile.target_salary_rule
    if rule is None:
        return ""
    title = (result.title or "").lower()
    # The notebook keyed this off the seniority sub-score specifically.
    seniority = result.breakdown.get("seniority", 5)
    if (any(word in title for word in rule.junior_title_words)
            or seniority >= rule.junior_if_seniority_at_least):
        return rule.junior_range
    return rule.default_range


def score_breakdown(profile: UserProfile, result: JobResult) -> str:
    """"Tech:9 Loc:10 Size:8 Sen:10 -> 9.4", using the profile's short labels."""
    if not result.breakdown:
        return ""
    parts = [f"{d.label}:{result.breakdown.get(d.key, '?')}" for d in profile.dimensions]
    return " ".join(parts) + f" → {result.score}"


# Each takes (profile, result, application, run_timestamp).
REGISTRY = {
    "run_date": lambda p, r, a, ts: ts,
    "verdict_display": lambda p, r, a, ts: p.verdict_labels.label(r.verdict),
    "published_date": lambda p, r, a, ts: (r.published or "")[:10],
    "source_label": lambda p, r, a, ts: (r.source or "").capitalize(),
    "score_breakdown": lambda p, r, a, ts: score_breakdown(p, r),
    "target_salary": lambda p, r, a, ts: target_salary(p, r),
    "priority": lambda p, r, a, ts: (
        a.priority if a and a.priority
        else p.export.priority_labels.get(priority_key(r.score, r.published), "")
    ),
    "application_status": lambda p, r, a, ts: (
        a.status if a and a.status else p.export.default_application_status
    ),
    "notes": lambda p, r, a, ts: (a.notes if a else ""),
}


def resolve(
    spec: str,
    profile: UserProfile,
    result: JobResult,
    application: Application | None,
    run_timestamp: str,
):
    """Turn one column spec into a cell value."""
    kind, _, name = spec.partition(":")

    if kind == "field":
        value = getattr(result, name, "")
        if isinstance(value, list):
            return " | ".join(str(v) for v in value if v)
        if isinstance(value, dict):
            return " | ".join(f"{k}={v}" for k, v in value.items())
        return value if value is not None else ""

    if kind == "computed":
        fn = REGISTRY.get(name)
        if fn is None:
            raise KeyError(
                f"unknown computed column {name!r}. "
                f"Known: {', '.join(sorted(REGISTRY))}"
            )
        return fn(profile, result, application, run_timestamp)

    if kind == "flag":
        # Yes/no in the profile's own language, matching the notebook's
        # "Catalán requerido" column.
        yes, no = ("Sí", "No") if profile.language in ("es", "ca") else ("Yes", "No")
        return yes if result.flags.get(name) else no

    raise KeyError(f"unknown column spec {spec!r}")
