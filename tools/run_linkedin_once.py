"""One authorised, real LinkedIn fetch — to measure the actor and seed a dump.

    python3 tools/run_linkedin_once.py

THIS SPENDS MONEY. It starts 12 Apify actor runs. It exists as a file, run
explicitly, precisely so it can never happen as a side effect of a test.

Goals, in order:
  1. Measure how many results this actor really returns per search URL. That
     number is the one unknown left in the cost guard: InfoJobs was measured at
     48/URL regardless of the cap it was asked for, and LinkedIn has never been
     measured at all.
  2. Save a raw dump so every later run of this connector can use
     reuse_dump=true and cost nothing.

The cost guard is not bypassed: the estimate is recomputed here and the run
proceeds only by passing back that estimate's own fingerprint.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from jobscout.collect import collect, plan_run  # noqa: E402
from jobscout.connectors.base import DumpStore, Secrets  # noqa: E402
from jobscout.profile import load_profile  # noqa: E402

from cost_guard import check_run_cost  # noqa: E402


def load_env(path: Path) -> dict[str, str]:
    """Minimal .env reader — avoids a dependency and never prints values."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def main() -> int:
    env = load_env(REPO / ".env")
    secrets = Secrets(apify_token=env.get("APIFY_TOKEN", ""))
    if not secrets.apify_token:
        print("APIFY_TOKEN missing from .env — aborting, nothing spent.")
        return 1
    print(f"APIFY_TOKEN loaded: {'yes' if secrets.apify_token else 'no'}")

    profile = load_profile(REPO / "profiles" / "gabriel.json", base_dir=REPO)
    dumps_dir = REPO / "dumps"

    # ---- re-confirm the cost immediately before spending -------------------
    plans, _ = plan_run(profile, dumps_dir)
    estimate = check_run_cost(plans, profile.cost_policy)
    print(f"\nGuard says: {estimate.decision.value} — ${estimate.usd_total:.2f}")
    print(f"Fingerprint: {estimate.fingerprint}")

    if estimate.decision.value == "REJECTED_OVER_HARD_CAP":
        print("Rejected over the hard cap. Nothing spent.")
        return 1

    li_cfg = profile.source("linkedin_apify")
    print(f"\nAbout to start {plans[0].n_starts} Apify actor runs "
          f"({len(li_cfg.keywords)} keywords x {len(li_cfg.work_types)} work types), "
          f"count={li_cfg.max_per_search} each.\n")

    # ---- the authorised run ------------------------------------------------
    result = collect(
        profile, secrets,
        confirmed_fingerprint=estimate.fingerprint,
        dumps_dir=dumps_dir,
    )
    print(f"\nRun status: {result.decision.value}")
    print(f"Counts: {result.counts}")

    # ---- the measurement ---------------------------------------------------
    store = DumpStore(dumps_dir)
    if not store.exists("linkedin_apify", "gabriel"):
        print("\nNo dump was written — the actor returned nothing.")
        return 1

    items, meta = store.load("linkedin_apify", "gabriel")
    per_url = Counter(item.get("_searchUrl", "?") for item in items)
    requested = li_cfg.max_per_search

    print("\n" + "=" * 74)
    print("MEASUREMENT — results actually returned per search URL")
    print("=" * 74)
    print(f"requested per URL (count) : {requested}")
    print(f"total items received      : {len(items)}")
    print(f"search URLs used          : {len(per_url)}")
    counts = sorted(per_url.values())
    print(f"per-URL counts            : {counts}")
    if counts:
        print(f"min / median / max        : "
              f"{counts[0]} / {counts[len(counts) // 2]} / {counts[-1]}")
        print(f"mean                      : {len(items) / len(per_url):.1f}")
        honoured = counts[-1] <= requested
        print(f"\nDoes the actor honour `count`? "
              f"{'YES' if honoured else 'NO'} "
              f"(max observed {counts[-1]} vs {requested} requested)")

    for url, n in per_url.most_common():
        print(f"  {n:>4}  {url[:96]}")

    summary = {
        "requested_per_url": requested,
        "total_items": len(items),
        "n_search_urls": len(per_url),
        "per_url_counts": counts,
        "max_observed_per_url": counts[-1] if counts else 0,
        "apify_run_meta": {k: v for k, v in meta.items() if k != "search_urls"},
    }
    out = REPO / "dumps" / "linkedin_measurement.json"
    out.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    print(f"\nMeasurement saved to {out.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
