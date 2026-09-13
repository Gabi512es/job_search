"""The collection phase: plan every source, ask the cost guard once, then fetch.

This is where cost_guard stops being a standalone helper and actually gates
real spending. The sequence is the one fixed in ARCHITECTURE.md § 3.1, and the
ordering is the safety property: no connector's fetch() is reachable until the
guard has returned OK.

The run is atomic. Free sources wait for the same green light as paid ones, so
a run is either complete or not started - never half a result set.

pipeline.run() (block 6) will call this, then score, store and export.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from jobscout.connectors import Secrets, SourceConnector, build_connectors
from jobscout.filters import apply_exclusions, deduplicate, filter_new
from jobscout.jobs import JobPosting
from jobscout.profile import UserProfile

from cost_guard import ConnectorPlan, CostDecision, RunEstimate, check_run_cost


@dataclass
class CollectionResult:
    """Everything the collection phase produced, including why it stopped."""

    decision: CostDecision
    cost: RunEstimate
    jobs: list[JobPosting] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def ran(self) -> bool:
        return self.decision is CostDecision.OK

    def summary(self) -> str:
        if not self.ran:
            return f"{self.decision.value}: {self.cost.reason}"
        parts = ", ".join(f"{k}={v}" for k, v in self.counts.items())
        return f"OK: {len(self.jobs)} jobs ready to score ({parts})"


def plan_run(
    profile: UserProfile, dumps_dir: Path | str = "dumps"
) -> tuple[list[ConnectorPlan], dict[str, SourceConnector]]:
    """Ask every enabled source what it intends to do. No network, no spend."""
    connectors = build_connectors(profile, dumps_dir)
    plans: list[ConnectorPlan] = []
    for cfg in profile.enabled_sources():
        connector = connectors.get(cfg.type)
        if connector is None:
            raise ValueError(f"no connector registered for source type {cfg.type!r}")
        plans.extend(connector.plan(cfg))
    return plans, connectors


def collect(
    profile: UserProfile,
    secrets: Secrets,
    *,
    seen_keys: set[str] | None = None,
    confirmed_fingerprint: str | None = None,
    dumps_dir: Path | str = "dumps",
) -> CollectionResult:
    """Plan, check cost, and only then fetch, deduplicate and filter.

    Returns without fetching anything when the guard withholds approval:
      NEEDS_CONFIRMATION     -> show result.cost.to_dict(), call again with
                                confirmed_fingerprint=result.cost.fingerprint
      REJECTED_OVER_HARD_CAP -> nothing to negotiate; shrink the plan
    """
    plans, connectors = plan_run(profile, dumps_dir)

    estimate = check_run_cost(
        plans, profile.cost_policy, confirmed_fingerprint=confirmed_fingerprint
    )
    if not estimate.may_run:
        # Nothing has been fetched. No connector has been touched.
        return CollectionResult(decision=estimate.decision, cost=estimate)

    # ---- green light -------------------------------------------------------
    jobs: list[JobPosting] = []
    per_source: dict[str, int] = {}
    for cfg in profile.enabled_sources():
        fetched = connectors[cfg.type].fetch(cfg, secrets)
        per_source[cfg.type] = len(fetched)
        jobs.extend(fetched)

    counts: dict[str, int] = {"fetched": len(jobs), **{f"src_{k}": v for k, v in per_source.items()}}

    jobs, n_dupes = deduplicate(jobs)
    counts["deduplicated"] = n_dupes

    jobs, excluded = apply_exclusions(jobs, profile.exclusions)
    counts["excluded"] = sum(excluded.values())
    counts.update({f"excl_{k}": v for k, v in excluded.items()})

    if seen_keys:
        jobs, n_seen = filter_new(jobs, seen_keys)
        counts["already_seen"] = n_seen

    counts["to_score"] = len(jobs)

    return CollectionResult(
        decision=estimate.decision, cost=estimate, jobs=jobs, counts=counts
    )
