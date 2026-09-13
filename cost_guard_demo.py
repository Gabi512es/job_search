"""Exercises cost_guard.py against the real parameters of both pipelines.

Runs offline: no network call, no Apify actor start, no money spent.
    python3 cost_guard_demo.py
"""

from cost_guard import (
    CostDecision,
    CostPolicy,
    UnknownActorError,
    ConnectorPlan,
    check_run_cost,
    plan_infojobs,
    plan_linkedin,
)


def show(title: str, estimate) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")
    print(f"decision  : {estimate.decision.value}")
    print(f"total     : ${estimate.usd_total:.2f}   "
          f"(cap ${estimate.policy.max_cost_per_run:.2f} / "
          f"confirm above ${estimate.policy.confirm_above:.2f})")
    print(f"starts    : {estimate.n_starts_total}")
    print(f"results   : {estimate.worst_case_results_total} (worst case)")
    print(f"fingerprint: {estimate.fingerprint}")
    print(f"may_run   : {estimate.may_run}")
    print(f"reason    : {estimate.reason}")
    for e in estimate.per_connector:
        print(f"\n  [{e.connector}] {e.actor_id}")
        print(f"    {e.n_starts} start(s) x ${e.usd_starts / max(e.n_starts, 1):.4f} "
              f"= ${e.usd_starts:.4f}")
        print(f"    {e.worst_case_results} result(s)  = ${e.usd_results:.4f}")
        print(f"    subtotal ${e.usd_total:.4f}   "
              f"(maxItems requested: {e.max_items_requested}, "
              f"search URLs: {e.n_search_urls})")
        for w in e.warnings:
            print(f"    /!\\ {w}")


# --- Case 1: Camila's InfoJobs run, exactly as configured today --------------
# SEARCH_KEYWORDS has 9 entries, MAX_JOBS_PER_SOURCE = 12.
show(
    "1. InfoJobs as configured today (9 keywords x 12 = maxItems 108)",
    check_run_cost([plan_infojobs(n_keywords=9, max_per_search=12)]),
)

# --- Case 2: Gabriel's LinkedIn run, exactly as configured today -------------
# LINKEDIN_KEYWORDS has 6 entries, work_types = ["hybrid", "onsite"],
# max_per_search = 100. This connector had NO cost guard at all.
show(
    "2. LinkedIn as configured today (6 keywords x 2 work types x count 100)",
    check_run_cost([plan_linkedin(n_keywords=6, n_work_types=2, max_per_search=100)]),
)

# --- Case 3: both connectors in one run, default policy ----------------------
both = [
    plan_infojobs(n_keywords=9, max_per_search=12),
    plan_linkedin(n_keywords=6, n_work_types=2, max_per_search=100),
]
est_both = check_run_cost(both)
show("3. Both connectors in one run (default policy)", est_both)

# --- Case 4: resuming case 3 with the correct confirmation -------------------
show(
    "4. Same run, replayed WITH the confirmation fingerprint",
    check_run_cost(both, confirmed_fingerprint=est_both.fingerprint),
)

# --- Case 5: a stale / forged confirmation must not be accepted --------------
show(
    "5. Same run, replayed with a WRONG fingerprint",
    check_run_cost(both, confirmed_fingerprint="0000deadbeef0000"),
)

# --- Case 6: hard cap. Confirmation must NOT rescue it ----------------------
tight = CostPolicy(max_cost_per_run=0.50, confirm_above=0.10)
est_capped = check_run_cost(both, policy=tight)
show("6. Same run against a $0.50 hard cap", est_capped)

show(
    "7. Rejected run, replayed WITH its own fingerprint (must stay rejected)",
    check_run_cost(both, policy=tight, confirmed_fingerprint=est_capped.fingerprint),
)

# --- Case 8: a cheap run just goes ------------------------------------------
show(
    "8. A small InfoJobs run (2 keywords x 10)",
    check_run_cost([plan_infojobs(n_keywords=2, max_per_search=10)]),
)

# --- Case 9: unpriced actor fails loudly instead of estimating $0 -----------
print(f"\n{'=' * 78}\n9. An actor with no pricing entry\n{'=' * 78}")
try:
    check_run_cost([ConnectorPlan(
        connector="some_new_source",
        actor_id="someone/brand-new-scraper",
        n_starts=1,
        n_search_urls=5,
        max_items_requested=500,
    )])
except UnknownActorError as e:
    print(f"UnknownActorError raised as intended:\n  {e}")

# --- Case 10: an incoherent policy is rejected at construction -------------
print(f"\n{'=' * 78}\n10. Incoherent policy (confirm_above > hard cap)\n{'=' * 78}")
try:
    CostPolicy(max_cost_per_run=1.00, confirm_above=2.00)
except ValueError as e:
    print(f"ValueError raised as intended:\n  {e}")

# --- Case 11: empty run -----------------------------------------------------
show("11. No connectors enabled at all", check_run_cost([]))

# --- Case 12: the three measurement states are distinguished ----------------
print(f"\n{'=' * 78}\n12. Measured cap behaviour drives the estimate\n{'=' * 78}")
from cost_guard import APIFY_PRICING, ActorPricing, worst_case_results

li = APIFY_PRICING["curious_coder/linkedin-jobs-scraper"]
ij = APIFY_PRICING["easyapi/infojobs-job-scraper"]

# LinkedIn: measured 2026-09-12 as honouring its cap -> request is the ceiling,
# and lowering the request must lower the estimate.
full = check_run_cost([plan_linkedin(6, 2, 100)]).per_connector[0]
half = check_run_cost([plan_linkedin(6, 2, 50)]).per_connector[0]
print(f"  linkedin count=100 -> {full.worst_case_results} results, ${full.usd_total:.2f}")
print(f"  linkedin count=50  -> {half.worst_case_results} results, ${half.usd_total:.2f}")
print(f"  warnings on a measured-honoured actor: {full.warnings}")
assert li.cap_honoured is True
assert full.worst_case_results == 1200
assert half.worst_case_results == 600, "a honoured cap must scale with the request"
assert full.warnings == [], "no 'never measured' warning once measured"

# InfoJobs: measured as IGNORING its cap -> the measured floor wins and the
# estimate does NOT fall when the request falls.
ij_full = check_run_cost([plan_infojobs(9, 12)]).per_connector[0]
ij_small = check_run_cost([plan_infojobs(9, 1)]).per_connector[0]
print(f"\n  infojobs maxItems=108 -> {ij_full.worst_case_results} results")
print(f"  infojobs maxItems=9   -> {ij_small.worst_case_results} results "
      f"(floor of 48/URL applies)")
assert ij.cap_honoured is False
assert ij_small.worst_case_results == 432, "the measured floor must not be undercut"
assert ij_full.warnings, "an actor that ignores its cap must still warn"

# An unmeasured actor still warns that the figure is a lower bound.
APIFY_PRICING["test/unmeasured"] = ActorPricing(
    actor_id="test/unmeasured", usd_per_start=0.0, usd_per_result=0.001,
    observed_results_per_url=None, source="test",
)
unmeasured = check_run_cost([ConnectorPlan(
    connector="t", actor_id="test/unmeasured",
    n_starts=1, n_search_urls=5, max_items_requested=500)]).per_connector[0]
assert "No measured per-URL result count" in unmeasured.warnings[0]
print(f"\n  unmeasured actor still warns: {unmeasured.warnings[0][:70]}...")
del APIFY_PRICING["test/unmeasured"]
print("\n  Three states distinguished correctly.")


# --- Sanity assertions ------------------------------------------------------
assert check_run_cost([plan_infojobs(2, 10)]).decision is CostDecision.OK
assert est_both.decision is CostDecision.NEEDS_CONFIRMATION
assert check_run_cost(both, confirmed_fingerprint=est_both.fingerprint).decision is CostDecision.OK
assert check_run_cost(both, confirmed_fingerprint="wrong").decision is CostDecision.NEEDS_CONFIRMATION
assert est_capped.decision is CostDecision.REJECTED_OVER_HARD_CAP
assert check_run_cost(
    both, policy=tight, confirmed_fingerprint=est_capped.fingerprint
).decision is CostDecision.REJECTED_OVER_HARD_CAP
print(f"\n{'=' * 78}\nAll assertions passed.\n{'=' * 78}")
