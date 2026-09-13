"""Persistence interface, and the row shape the app actually consumes.

`job_results` is the deliverable (ARCHITECTURE.md § 8): the frontend reads that
table to show a user their offers. The Excel export is a convenience built on
top of the same rows, not the mechanism of record.

The Store protocol is deliberately small and explicit. There is no global
state, no module-level path, and every method takes the user it acts for, so
two users can be processed concurrently without interfering.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from pydantic import BaseModel, Field

from jobscout.jobs import JobPosting
from jobscout.profile import UserProfile, Verdict
from jobscout.scoring import ScoredJob


class JobResult(BaseModel):
    """One row of `job_results`. Mirrors the column list in ARCHITECTURE.md 5.3.

    `user_id` + `url` is the natural key: the same posting must not appear
    twice for one user, whichever run found it. That uniqueness replaces the
    old `load_tracker_urls()`, which re-read an .xlsx file to avoid re-scoring.
    """

    user_id: str
    run_id: str
    job_key: str

    title: str = ""
    company: str = ""
    url: str = ""
    source: str = ""
    published: str = ""
    location: str = ""

    verdict: Verdict = "NO"
    score: float = 0.0
    base_score: float = 0.0
    breakdown: dict[str, int] = Field(default_factory=dict)   # jsonb

    one_liner: str = ""
    match_signals: list[str] = Field(default_factory=list)    # text[]
    gaps: list[str] = Field(default_factory=list)             # text[]
    red_flags: list[str] = Field(default_factory=list)        # text[]
    flags: dict[str, bool] = Field(default_factory=dict)      # jsonb
    extra: dict[str, Any] = Field(default_factory=dict)       # jsonb

    generated_text: str = ""
    salary_range_market: str = ""
    evaluated_at: str = ""


class Application(BaseModel):
    """One row of `applications` — the user's own tracking of a result.

    Separate from JobResult on purpose. In the notebooks the Tracker sheet was
    deleted and rebuilt on every run, so any status the user had edited by hand
    was silently overwritten. Keeping it in its own table means a run can add
    results without touching what the user wrote.
    """

    user_id: str
    job_url: str          # references job_results(url) for this user
    status: str = ""
    priority: str = ""
    target_salary: str = ""
    notes: str = ""
    updated_at: str = ""


class RunRecord(BaseModel):
    """One row of `runs`.

    `cost_fingerprint` is stored so the second confirmation of a held run can
    arrive in a later, separate request - the server does not need to keep the
    estimate in memory between the two.
    """

    run_id: str
    user_id: str
    profile_id: str
    started_at: str = ""
    finished_at: str = ""
    status: str = ""
    cost_decision: str = ""
    cost_estimate: dict[str, Any] = Field(default_factory=dict)
    cost_fingerprint: str = ""
    counts: dict[str, int] = Field(default_factory=dict)


def to_job_result(
    scored: ScoredJob, job: JobPosting, user_id: str, run_id: str
) -> JobResult:
    """Map a scored posting onto the row the app will read.

    The JobPosting is passed alongside because `job_key` is the deduplication
    identity, which the ScoredJob does not carry.
    """
    return JobResult(
        user_id=user_id,
        run_id=run_id,
        job_key=job.key,
        title=scored.title,
        company=scored.company,
        url=scored.url,
        source=scored.source,
        published=scored.published,
        location=scored.location,
        verdict=scored.verdict,
        score=scored.score,
        base_score=scored.base_score,
        breakdown=dict(scored.sub_scores),
        one_liner=scored.one_liner,
        match_signals=list(scored.match_signals),
        gaps=list(scored.gaps),
        red_flags=list(scored.red_flags),
        flags=dict(scored.flags),
        extra=dict(scored.extra),
        generated_text=scored.generated_text,
        salary_range_market=scored.salary_range_market,
        evaluated_at=scored.evaluated_at or datetime.now().isoformat(),
    )


class Store(Protocol):
    """What the pipeline needs from persistence. Implemented by JsonStore now,
    by SupabaseStore later."""

    # -- seen_jobs ----------------------------------------------------------
    def seen_keys(self, user_id: str) -> set[str]:
        """Job keys this user has already been shown."""
        ...

    def mark_seen(self, user_id: str, keys: set[str]) -> None:
        ...

    # -- job_results --------------------------------------------------------
    def known_urls(self, user_id: str) -> set[str]:
        """URLs already scored for this user, so they are not scored again."""
        ...

    def save_results(self, user_id: str, results: list[JobResult]) -> int:
        """Insert results, skipping any URL this user already has.

        Returns the number actually inserted.
        """
        ...

    def get_results(
        self, user_id: str, verdicts: list[Verdict] | None = None
    ) -> list[JobResult]:
        ...

    # -- runs ---------------------------------------------------------------
    def save_run(self, record: RunRecord) -> None:
        ...

    def get_run(self, user_id: str, run_id: str) -> RunRecord | None:
        ...

    # -- applications -------------------------------------------------------
    def get_applications(self, user_id: str) -> dict[str, Application]:
        """Keyed by job_url, so an export can look up a result's status."""
        ...

    def save_application(self, application: Application) -> None:
        ...
