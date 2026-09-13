"""One authorised, real end-to-end run for both profiles.

    python3 -u tools/run_real_once.py

THIS SPENDS MONEY: Haiku for scoring, Sonnet for application texts. Approved
scenario: Apify connectors replay their saved dumps (so $0.00 there), 25
postings scored per profile, text generation on. Estimated at $0.75 worst case.

A separate file, run explicitly, so a billed run can never happen as a side
effect of a test.

Exports go to exports/ rather than over job_scout_results.xlsx: those files are
still the reference data for tests/test_scoring.py and
tests/test_store_export.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from jobscout.connectors import Secrets  # noqa: E402
from jobscout.pipeline import RunOptions, run  # noqa: E402
from jobscout.profile import load_profile  # noqa: E402
from jobscout.store.json_store import JsonStore  # noqa: E402

MAX_JOBS = 25


def replaying(profile):
    """Same profile with every paid connector replaying its dump."""
    return profile.model_copy(update={"sources": [
        s.model_copy(update={"reuse_dump": True})
        if s.type in ("infojobs_apify", "linkedin_apify") else s
        for s in profile.sources
    ]})


def main() -> int:
    secrets = Secrets.from_env()
    if not secrets.anthropic_api_key:
        print("ANTHROPIC_API_KEY missing — aborting, nothing spent.")
        return 1

    store = JsonStore(REPO / "store_data")
    exports = REPO / "exports"
    exports.mkdir(exist_ok=True)

    reports = {}
    for name in ("gabriel", "camila"):
        profile = load_profile(REPO / "profiles" / f"{name}.json", base_dir=REPO)
        print(f"\n{'=' * 74}\n{name.upper()}\n{'=' * 74}")

        report = run(
            replaying(profile), store, secrets,
            RunOptions(
                max_jobs_to_score=MAX_JOBS,
                dumps_dir=str(REPO / "dumps"),
                export_xlsx_path=str(exports / f"{name}.xlsx"),
            ),
            repo_dir=REPO,
        )
        reports[name] = report
        print(f"\nstatus : {report.status}")
        for key, value in report.counts.items():
            print(f"   {key:20} {value}")

    print(f"\n{'=' * 74}\nDONE\n{'=' * 74}")
    for name, report in reports.items():
        print(f"  {name}: run {report.run_id}, "
              f"{report.counts.get('saved', 0)} rows saved, "
              f"export {report.export_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
