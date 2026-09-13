"""Apify cost guard — decides whether a scraping run is allowed to spend money.

Replaces the blocking `input()` prompt that used to live inside the InfoJobs
connector. That prompt cannot work in a backend called by an HTTP API: it would
freeze the request until someone typed into a terminal that nobody is watching.

Instead, this module *returns a decision*. It never blocks, never asks a
question, and never raises on a cost that is merely high:

    OK                      -> the caller may run
    NEEDS_CONFIRMATION      -> the caller must show the estimate to the user and
                               call again with the confirmation fingerprint
    REJECTED_OVER_HARD_CAP  -> the caller must NOT run. No confirmation can
                               override this.

The same guard applies to every Apify connector, not just InfoJobs.

Nothing here performs any network call or starts any actor.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from enum import Enum


# ---------------------------------------------------------------------------
# 1. Decision statuses
# ---------------------------------------------------------------------------

class CostDecision(str, Enum):
    """Outcome of a cost check. Subclasses `str` so it serialises to JSON as-is."""

    OK = "OK"
    NEEDS_CONFIRMATION = "NEEDS_CONFIRMATION"
    REJECTED_OVER_HARD_CAP = "REJECTED_OVER_HARD_CAP"


# ---------------------------------------------------------------------------
# 2. Actor pricing
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ActorPricing:
    """What one Apify actor charges, and how it behaves when asked for a cap.

    Three distinct states, because conflating them either under-estimates cost
    or over-estimates it:

    1. `cap_honoured=True` — measured, and the actor returns at most what it is
       asked for. The requested cap IS the ceiling.
    2. `observed_results_per_url=N` — measured, and the actor returns N results
       per search URL *regardless* of the cap. The cap is meaningless; N is.
    3. neither set — never measured. The estimate is a lower bound and says so.

    Encoding a honoured cap as `observed_results_per_url=<the cap we happened
    to request>` would be wrong: it would then be treated as an independent
    floor and over-estimate every smaller request.
    """

    actor_id: str
    usd_per_start: float
    usd_per_result: float
    observed_results_per_url: int | None
    source: str
    cap_honoured: bool | None = None


# Verified against each actor's public Apify page on 2026-09-09.
#
# Caveat that applies to both: Apify states that pay-per-event actors may also
# bill platform usage on top of the per-event price. These figures are therefore
# a good floor, not a guaranteed ceiling. Keep the hard cap comfortably low.
APIFY_PRICING: dict[str, ActorPricing] = {
    "easyapi/infojobs-job-scraper": ActorPricing(
        actor_id="easyapi/infojobs-job-scraper",
        usd_per_start=0.09,
        usd_per_result=0.00299,  # page states "from $2.99 / 1,000 results"
        # A real run on 2026-07-19 asked for maxItems=108 across 9 search URLs
        # and received 432 items — exactly 48 per URL. maxItems did not cap it.
        observed_results_per_url=48,
        cap_honoured=False,
        source="apify.com/easyapi/infojobs-job-scraper + measured run 2026-07-19",
    ),
    "curious_coder/linkedin-jobs-scraper": ActorPricing(
        actor_id="curious_coder/linkedin-jobs-scraper",
        usd_per_start=0.0,  # no per-start fee published for this actor
        usd_per_result=0.001,  # page states "from $1.00 / 1,000 results"
        # MEASURED 2026-09-12 on a real 12-URL run: every one of the 12 search
        # URLs returned exactly 100 results for count=100. This actor honours
        # its cap, unlike easyapi/infojobs-job-scraper.
        observed_results_per_url=None,
        cap_honoured=True,
        source=(
            "apify.com/curious_coder/linkedin-jobs-scraper + measured run "
            "2026-09-12: 12 URLs x count=100 -> 1200 items, per-URL counts all 100"
        ),
    ),
}


class UnknownActorError(ValueError):
    """Raised when a plan references an actor with no pricing entry.

    This is a configuration mistake, not a runtime decision: refusing loudly is
    safer than estimating a cost of $0 for an actor that in fact bills money.
    """


# ---------------------------------------------------------------------------
# 3. Policy
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CostPolicy:
    """Spending limits for one run. Per user, or the application default.

    `max_cost_per_run` is a hard cap: above it the run is rejected outright and
    no confirmation can rescue it.

    `confirm_above` is a vigilance threshold: between it and the hard cap, the
    run is held pending an explicit confirmation from the user.
    """

    max_cost_per_run: float = 5.00
    confirm_above: float = 1.00  # historical default from the InfoJobs connector

    def __post_init__(self) -> None:
        if self.confirm_above > self.max_cost_per_run:
            raise ValueError(
                f"confirm_above ({self.confirm_above}) cannot exceed "
                f"max_cost_per_run ({self.max_cost_per_run}): the confirmation "
                f"window would be empty and every non-trivial run would be rejected."
            )


# ---------------------------------------------------------------------------
# 4. What a connector intends to do
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ConnectorPlan:
    """A connector's intent, declared *before* it spends anything.

    Every Apify connector must be able to produce this without calling Apify:
    it is just arithmetic over its own search parameters.
    """

    connector: str          # "infojobs", "linkedin", ...
    actor_id: str
    n_starts: int           # how many actor.call() invocations
    n_search_urls: int      # how many distinct search URLs in total
    max_items_requested: int  # the cap we ask the actor to respect


@dataclass(frozen=True)
class ConnectorEstimate:
    """Cost estimate for one connector plan."""

    connector: str
    actor_id: str
    n_starts: int
    n_search_urls: int
    max_items_requested: int
    worst_case_results: int
    usd_starts: float
    usd_results: float
    usd_total: float
    warnings: list[str] = field(default_factory=list)


def worst_case_results(plan: ConnectorPlan, pricing: ActorPricing) -> tuple[int, list[str]]:
    """Generalised version of the original InfoJobs formula.

        worst_case = max(max_items_requested, observed_per_url * n_search_urls)

    We bill on whichever is larger: what we asked for, or what the actor has
    actually been seen to return. When no measurement exists we fall back to the
    requested cap and say so loudly, because that is precisely the assumption
    that under-estimated the InfoJobs run by 4x.

    When a measurement shows the actor DOES honour its cap (`cap_honoured`),
    the requested cap is the ceiling and no warning is emitted.
    """
    warnings: list[str] = []

    # 1. Measured, and the cap is respected: the request IS the ceiling.
    if pricing.cap_honoured:
        return plan.max_items_requested, warnings

    # 2. Never measured: say so, and do not pretend the cap is a ceiling.
    if pricing.observed_results_per_url is None:
        warnings.append(
            f"No measured per-URL result count for {pricing.actor_id}. This "
            f"estimate assumes the requested cap of {plan.max_items_requested} "
            f"is honoured. That assumption proved FALSE for "
            f"easyapi/infojobs-job-scraper (108 requested, 432 received). "
            f"Treat this figure as a lower bound until a real run is measured."
        )
        return plan.max_items_requested, warnings

    # 3. Measured, and the cap is ignored: bill on the measured figure.
    observed_total = pricing.observed_results_per_url * plan.n_search_urls
    if observed_total > plan.max_items_requested:
        warnings.append(
            f"maxItems={plan.max_items_requested} requested, but {pricing.actor_id} "
            f"has been measured returning {pricing.observed_results_per_url} results "
            f"per search URL regardless of that cap "
            f"({pricing.observed_results_per_url} x {plan.n_search_urls} URLs = "
            f"{observed_total}). Billing on the measured figure."
        )
    return max(plan.max_items_requested, observed_total), warnings


def estimate_connector(plan: ConnectorPlan) -> ConnectorEstimate:
    """Price a single connector plan. No network call."""
    pricing = APIFY_PRICING.get(plan.actor_id)
    if pricing is None:
        raise UnknownActorError(
            f"No pricing entry for Apify actor {plan.actor_id!r}. Add one to "
            f"APIFY_PRICING (read the numbers off the actor's Apify page) before "
            f"running it. Refusing to estimate its cost as zero."
        )

    results, warnings = worst_case_results(plan, pricing)
    usd_starts = plan.n_starts * pricing.usd_per_start
    usd_results = results * pricing.usd_per_result

    return ConnectorEstimate(
        connector=plan.connector,
        actor_id=plan.actor_id,
        n_starts=plan.n_starts,
        n_search_urls=plan.n_search_urls,
        max_items_requested=plan.max_items_requested,
        worst_case_results=results,
        usd_starts=round(usd_starts, 4),
        usd_results=round(usd_results, 4),
        usd_total=round(usd_starts + usd_results, 4),
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# 5. The run-level decision
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RunEstimate:
    """The guard's answer. This is what a backend hands to its frontend."""

    decision: CostDecision
    reason: str
    usd_total: float
    n_starts_total: int
    worst_case_results_total: int
    per_connector: list[ConnectorEstimate]
    policy: CostPolicy
    # Identifies exactly this plan + policy. A confirmation is only valid for
    # the fingerprint it was issued for, so a user cannot approve a $0.50 run
    # and have a $4.00 run executed under that approval.
    fingerprint: str

    @property
    def may_run(self) -> bool:
        return self.decision is CostDecision.OK

    def to_dict(self) -> dict:
        """JSON-serialisable form, ready to return from an API endpoint."""
        return asdict(self) | {
            "decision": self.decision.value,
            "may_run": self.may_run,
        }


def _fingerprint(plans: list[ConnectorPlan], policy: CostPolicy, usd_total: float) -> str:
    payload = {
        # Sorted so that the same set of plans in a different order yields the
        # same fingerprint. Dicts are not orderable, so sort on their JSON form.
        "plans": sorted(
            (asdict(p) for p in plans),
            key=lambda d: json.dumps(d, sort_keys=True),
        ),
        "policy": asdict(policy),
        "usd_total": round(usd_total, 4),
    }
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def check_run_cost(
    plans: list[ConnectorPlan],
    policy: CostPolicy | None = None,
    confirmed_fingerprint: str | None = None,
) -> RunEstimate:
    """Decide whether a run may spend money. Never blocks, never prompts.

    Call it once with no `confirmed_fingerprint`:
      - OK                     -> run it.
      - NEEDS_CONFIRMATION     -> show `.to_dict()` to the user, then call again
                                  passing `confirmed_fingerprint=estimate.fingerprint`.
      - REJECTED_OVER_HARD_CAP -> do not run. Shrink the plan or raise the cap.

    Passing a matching `confirmed_fingerprint` turns NEEDS_CONFIRMATION into OK.
    It can never turn REJECTED_OVER_HARD_CAP into OK.
    """
    policy = policy or CostPolicy()

    if not plans:
        return RunEstimate(
            decision=CostDecision.OK,
            reason="Nothing to run: no connector plans submitted.",
            usd_total=0.0,
            n_starts_total=0,
            worst_case_results_total=0,
            per_connector=[],
            policy=policy,
            fingerprint=_fingerprint([], policy, 0.0),
        )

    estimates = [estimate_connector(p) for p in plans]
    usd_total = round(sum(e.usd_total for e in estimates), 4)
    starts_total = sum(e.n_starts for e in estimates)
    results_total = sum(e.worst_case_results for e in estimates)
    fingerprint = _fingerprint(plans, policy, usd_total)

    breakdown = ", ".join(f"{e.connector} ${e.usd_total:.2f}" for e in estimates)

    # 1. Hard cap first. Non-negotiable, checked before any confirmation.
    if usd_total > policy.max_cost_per_run:
        decision = CostDecision.REJECTED_OVER_HARD_CAP
        reason = (
            f"Estimated ${usd_total:.2f} exceeds the hard cap of "
            f"${policy.max_cost_per_run:.2f}. Run cancelled; confirmation is not "
            f"offered for this case. Breakdown: {breakdown}."
        )

    # 2. Vigilance window: hold for an explicit, plan-specific confirmation.
    elif usd_total > policy.confirm_above:
        if confirmed_fingerprint == fingerprint:
            decision = CostDecision.OK
            reason = (
                f"Estimated ${usd_total:.2f} is above the ${policy.confirm_above:.2f} "
                f"confirmation threshold but was explicitly confirmed for this exact "
                f"plan. Breakdown: {breakdown}."
            )
        else:
            decision = CostDecision.NEEDS_CONFIRMATION
            stale = (
                " The confirmation supplied was issued for a different plan or policy."
                if confirmed_fingerprint else ""
            )
            reason = (
                f"Estimated ${usd_total:.2f} is above the ${policy.confirm_above:.2f} "
                f"confirmation threshold and below the ${policy.max_cost_per_run:.2f} "
                f"hard cap. Explicit user confirmation required.{stale} "
                f"Breakdown: {breakdown}."
            )

    # 3. Below the vigilance threshold: just run.
    else:
        decision = CostDecision.OK
        reason = (
            f"Estimated ${usd_total:.2f} is at or below the "
            f"${policy.confirm_above:.2f} confirmation threshold. Breakdown: {breakdown}."
        )

    return RunEstimate(
        decision=decision,
        reason=reason,
        usd_total=usd_total,
        n_starts_total=starts_total,
        worst_case_results_total=results_total,
        per_connector=estimates,
        policy=policy,
        fingerprint=fingerprint,
    )


# ---------------------------------------------------------------------------
# 6. Plan builders for the two existing Apify connectors
# ---------------------------------------------------------------------------
# These mirror exactly what each connector does today, so the guard prices the
# real behaviour rather than an idealised version of it.

def plan_infojobs(n_keywords: int, max_per_search: int) -> ConnectorPlan:
    """InfoJobs batches every keyword into ONE actor start via `searchUrls`."""
    return ConnectorPlan(
        connector="infojobs",
        actor_id="easyapi/infojobs-job-scraper",
        n_starts=1,
        n_search_urls=n_keywords,
        max_items_requested=max_per_search * n_keywords,
    )


def plan_linkedin(n_keywords: int, n_work_types: int, max_per_search: int) -> ConnectorPlan:
    """LinkedIn starts the actor once per (keyword x work_type) pair — no batching.

    This is the connector that had no cost guard at all.
    """
    n_starts = n_keywords * n_work_types
    return ConnectorPlan(
        connector="linkedin",
        actor_id="curious_coder/linkedin-jobs-scraper",
        n_starts=n_starts,
        n_search_urls=n_starts,  # one search URL per start
        max_items_requested=max_per_search * n_starts,
    )
