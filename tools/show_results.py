"""Print a readable sample of what is actually stored in job_results.

    python3 tools/show_results.py [n_per_verdict]

Reads the store only. No network, no API call, no cost. This is the table the
Lovable frontend will query, so what it prints is what the app would show.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from jobscout.profile import load_profile  # noqa: E402
from jobscout.store.json_store import JsonStore  # noqa: E402


def show(name: str, per_verdict: int) -> None:
    profile = load_profile(REPO / "profiles" / f"{name}.json", base_dir=REPO)
    store = JsonStore(REPO / "store_data")
    rows = store.get_results(name)

    print(f"\n{'=' * 78}\n{name.upper()} — {len(rows)} rows in job_results\n{'=' * 78}")
    if not rows:
        print("  (empty)")
        return

    counts = {v: sum(1 for r in rows if r.verdict == v) for v in ("YES", "MAYBE", "NO")}
    print(f"  verdicts: " + "  ".join(
        f"{profile.verdict_labels.label(v)}={n}" for v, n in counts.items()))
    print(f"  scores  : min {min(r.score for r in rows)}  "
          f"max {max(r.score for r in rows)}  "
          f"mean {sum(r.score for r in rows) / len(rows):.2f}")
    with_text = sum(1 for r in rows if r.generated_text)
    print(f"  generated texts: {with_text}")

    labels = {d.key: d.label for d in profile.dimensions}

    for verdict in ("YES", "MAYBE", "NO"):
        sample = sorted([r for r in rows if r.verdict == verdict],
                        key=lambda r: -r.score)[:per_verdict]
        if not sample:
            continue
        print(f"\n  --- {profile.verdict_labels.label(verdict)} "
              f"({counts[verdict]}) {'-' * 40}")
        for r in sample:
            breakdown = "  ".join(
                f"{labels[k]}:{v}" for k, v in r.breakdown.items() if k in labels)
            print(f"\n  [{r.score:>4}/10]  {r.title[:60]}")
            print(f"            {r.company[:44]}  |  {r.location or '?'}  |  {r.source}")
            print(f"            base {r.base_score}  ->  {r.score}   {breakdown}")
            if r.flags and any(r.flags.values()):
                fired = [k for k, v in r.flags.items() if v]
                print(f"            flags: {', '.join(fired)}")
            print(f"            {r.one_liner[:92]}")
            if r.match_signals:
                print(f"            + {' | '.join(r.match_signals[:2])[:88]}")
            if r.red_flags:
                print(f"            ! {' | '.join(r.red_flags[:2])[:88]}")
            if r.salary_range_market:
                print(f"            marché: {r.salary_range_market}")
            if r.generated_text:
                first = r.generated_text.split("\n")[0][:80]
                print(f"            texte ({len(r.generated_text)} chars): {first}...")
            print(f"            {r.url[:88]}")


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    for profile_name in ("gabriel", "camila"):
        show(profile_name, n)
