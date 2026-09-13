"""Proves the new profiles lost nothing from the original notebooks.

Run it:

    python3 tests/test_profile_completeness.py

Every expected value is read live from `job_scout.ipynb` and
`job_scout_camila/job_scout_camila.ipynb` by tests/legacy_values.py — none are
retyped here. So this is a real comparison against the source of truth, not a
restatement of the adapter's own output.

Exit code 0 = nothing lost. Non-zero = something was dropped or changed.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from jobscout.profile import load_profile  # noqa: E402
from tests import legacy_values as legacy  # noqa: E402

CHECKS = 0
FAILURES: list[str] = []


def check(label: str, actual, expected) -> None:
    """Compare and record. Never aborts, so one run reports every problem."""
    global CHECKS
    CHECKS += 1
    if actual == expected:
        detail = f"{len(actual)} items" if isinstance(actual, (list, set, dict)) else actual
        print(f"  OK    {label}: {detail}")
        return
    FAILURES.append(label)
    print(f"  FAIL  {label}")
    if isinstance(actual, (list, set)) and isinstance(expected, (list, set)):
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        if missing:
            print(f"          missing from profile : {missing}")
        if extra:
            print(f"          not in notebook      : {extra}")
        if not missing and not extra:
            print(f"          same members, different order")
            print(f"          notebook: {list(expected)}")
            print(f"          profile : {list(actual)}")
    else:
        print(f"          notebook: {expected!r}")
        print(f"          profile : {actual!r}")


def rule(profile, kind: str, index: int = 0):
    matches = [r for r in profile.exclusions if r.kind == kind]
    assert matches, f"no exclusion rule of kind {kind!r} in {profile.profile_id}"
    return matches[index]


# ===========================================================================
print("=" * 78)
print("GABRIEL — profiles/gabriel.json vs job_scout.ipynb")
print("=" * 78)

g = load_profile(REPO / "profiles" / "gabriel.json", base_dir=REPO)
gl = legacy.gabriel()

check("dimension keys", g.dimension_keys, list(gl["weights"]))
check("dimension weights", g.weights, gl["weights"])
check("weights sum to 1.0", round(sum(g.weights.values()), 6), 1.0)
check("threshold yes_above", g.thresholds.yes_above, gl["thresholds"]["yes_above"])
check("threshold maybe_above", g.thresholds.maybe_above, gl["thresholds"]["maybe_above"])
check("exclusion keywords", rule(g, "any_keyword").keywords, gl["excluded_keywords"])
check("exclusion fields", rule(g, "any_keyword").fields, ["title", "company"])
check("linkedin keywords", g.source("linkedin_apify").keywords, gl["linkedin_keywords"])
check("no llm_flags (none existed)", g.llm_flags, [])
check("no keyword_penalties (none existed)", g.keyword_penalties, [])

# Every rubric must carry real content from the prompt, not a placeholder.
for d in g.dimensions:
    check(f"rubric {d.key} non-trivial", len(d.rubric) > 150, True)
    check(f"rubric {d.key} has 5 bands",
          sum(d.rubric.count(b) for b in ("9-10:", "7-8:", "5-6:", "3-4:", "0-2:")), 5)

# RSS: the two feeds measured dead on 2026-09-09 are intentionally dropped.
DEAD_FEEDS = {
    "https://www.arbeitnow.com/feed",                      # 301 -> HTML homepage
    "https://remotive.com/remote-jobs/software-dev/feed",  # 404
}
notebook_feeds = gl["rss_feeds"]
live_expected = [f for f in notebook_feeds if f not in DEAD_FEEDS]
check("rss feeds (dead ones dropped)", g.source("rss").feeds, live_expected)
check("rss feeds dropped exactly 2", len(notebook_feeds) - len(g.source("rss").feeds), 2)

# Sources enabled per user — the requirement that a French user does not run
# the Spain-only connectors.
check("enabled sources", sorted(s.type for s in g.enabled_sources()),
      ["linkedin_apify", "rss"])
check("infojobs disabled", g.source("infojobs_apify").enabled, False)
check("xarxanet disabled", g.source("xarxanet").enabled, False)

check("verdict labels", [g.verdict_labels.yes, g.verdict_labels.maybe, g.verdict_labels.no],
      ["OUI", "OPPORTUNISTE", "NON"])
check("target_salary_rule kept", g.target_salary_rule is not None, True)
check("writing kind", g.writing.kind, "cover_letter")
check("writing min_words", g.writing.min_words, 550)
check("cv loads", len(g.load_cv(REPO)) > 500, True)

# ===========================================================================
print()
print("=" * 78)
print("CAMILA — profiles/camila.json vs job_scout_camila.ipynb")
print("=" * 78)

c = load_profile(REPO / "profiles" / "camila.json", base_dir=REPO)
cl = legacy.camila()

check("dimension keys", c.dimension_keys, list(cl["weights"]))
check("dimension weights", c.weights, cl["weights"])
check("weights sum to 1.0", round(sum(c.weights.values()), 6), 1.0)
check("threshold yes_above", c.thresholds.yes_above, cl["thresholds"]["yes_above"])
check("threshold maybe_above", c.thresholds.maybe_above, cl["thresholds"]["maybe_above"])

# All six adjustments, whether they came from an llm_flag or a keyword_penalty.
check("all flag/penalty adjustments", c.flag_adjustments, cl["flag_adjustments"])
check("llm_flags count", len(c.llm_flags), 5)
check("keyword_penalties count", len(c.keyword_penalties), 1)
check("maresme is a Python penalty, not an llm_flag",
      [p.key for p in c.keyword_penalties], ["corredor_maresme_r1"])
check("maresme towns", c.keyword_penalties[0].keywords, cl["maresme_towns"])
check("maresme searches location+title", c.keyword_penalties[0].fields,
      ["location", "title"])
check("bonus targets match_signals",
      [f.target for f in c.llm_flags if f.adjustment > 0], ["match_signals"])

for f in c.llm_flags:
    check(f"flag {f.key} has a definition", len(f.definition) > 40, True)
    check(f"flag {f.key} has a note", bool(f.note.strip()), True)

check("hard exclusion keywords", rule(c, "any_keyword").keywords, cl["excluded_keywords"])
check("voluntariado keywords", rule(c, "keyword_unless").keywords, cl["voluntariado_words"])
check("voluntariado unless", rule(c, "keyword_unless").unless_keywords,
      ["contrato", "contracte"])
check("city deny list", rule(c, "location_not_in").deny, cl["other_cities"])
check("city allow list", rule(c, "location_not_in").allow,
      ["barcelona", "bcn", "remote", "remoto", "remot", "teletrab"])
check("exclusion rule count", len(c.exclusions), 3)

check("infojobs keywords", c.source("infojobs_apify").keywords, cl["search_keywords"])
check("xarxanet filters", c.source("xarxanet").filter_keywords,
      cl["xarxanet_filter_keywords"])
check("infojobs filters", c.source("infojobs_apify").filter_keywords,
      cl["xarxanet_filter_keywords"])
check("infojobs max_per_search", c.source("infojobs_apify").max_per_search, 12)
check("enabled sources", sorted(s.type for s in c.enabled_sources()),
      ["infojobs_apify", "xarxanet"])
check("rss disabled", c.source("rss").enabled, False)
check("linkedin disabled", c.source("linkedin_apify").enabled, False)

check("verdict labels", [c.verdict_labels.yes, c.verdict_labels.maybe, c.verdict_labels.no],
      ["SÍ", "OPORTUNISTA", "NO"])
check("no target_salary_rule", c.target_salary_rule, None)
check("writing kind", c.writing.kind, "email")
check("writing max_words", c.writing.max_words, 150)
check("writing generates for YES+MAYBE", c.writing.generate_for_verdicts,
      ["YES", "MAYBE"])
check("cv loads", len(c.load_cv(REPO)) > 500, True)

for d in c.dimensions:
    check(f"rubric {d.key} non-trivial", len(d.rubric) > 150, True)

# ===========================================================================
print()
print("=" * 78)
print("EXPORT COLUMNS — the notebooks' COLUMNS lists, as data")
print("=" * 78)

check("gabriel Job Scout columns", len(g.export.sheets[0].columns), 16)
check("gabriel Tracker columns", len(g.export.sheets[1].columns), 9)
check("gabriel URL is last column", g.export.sheets[0].columns[-1].value, "field:url")
check("camila Job Scout columns", len(c.export.sheets[0].columns), 14)
check("camila Tracker columns", len(c.export.sheets[1].columns), 11)
check("camila Enlace is last column", c.export.sheets[0].columns[-1].value, "field:url")
check("camila Tracker exposes the catalan flag",
      [col.value for col in c.export.sheets[1].columns if col.value.startswith("flag:")],
      ["flag:catalan_imprescindible"])
check("tracker sheets are YES-only",
      [g.export.sheets[1].only_verdicts, c.export.sheets[1].only_verdicts],
      [["YES"], ["YES"]])

# Added in block 5: these were missed on the first pass. The notebooks'
# _tracker_priority returned emoji for Gabriel and Spanish words for Camila,
# and each wrote a different default status into the Tracker sheet.
check("gabriel priority labels are his emoji",
      [g.export.priority_labels[k] for k in ("high", "medium", "low")],
      ["\U0001F534", "\U0001F7E0", "\U0001F7E1"])
check("camila priority labels are her words",
      [c.export.priority_labels[k] for k in ("high", "medium", "low")],
      ["Alta", "Media", "Baja"])
check("gabriel default application status",
      g.export.default_application_status, "\u00c0 postuler")
check("camila default application status",
      c.export.default_application_status, "Pendiente")
check("the labels differ between profiles",
      g.export.priority_labels != c.export.priority_labels, True)

# ===========================================================================
print()
print("=" * 78)
print("ROUND-TRIP + SCHEMA GUARDS")
print("=" * 78)

from jobscout.profile import UserProfile  # noqa: E402
from pydantic import ValidationError  # noqa: E402

for p in (g, c):
    reloaded = UserProfile.model_validate(p.model_dump())
    # Compare as a boolean: printing the models themselves would dump the
    # candidate's personal data into the test output.
    check(f"{p.profile_id} survives a JSON round-trip", reloaded == p, True)

check("example profile is fictional and valid",
      load_profile(REPO / "profiles" / "example.json", base_dir=REPO).profile_id, "example")

# The validators must actually reject bad input, or they are decoration.
def rejects(label: str, mutate) -> None:
    global CHECKS
    CHECKS += 1
    payload = g.model_dump()
    mutate(payload)
    try:
        UserProfile.model_validate(payload)
    except ValidationError:
        print(f"  OK    rejects {label}")
        return
    FAILURES.append(f"does not reject {label}")
    print(f"  FAIL  accepted invalid profile: {label}")


def _break_weights(p):
    p["dimensions"][0]["weight"] = 0.99


def _dupe_dimension(p):
    p["dimensions"].append(dict(p["dimensions"][0]))


def _bad_thresholds(p):
    p["thresholds"] = {"yes_above": 5.0, "maybe_above": 7.0}


def _enabled_but_empty(p):
    for s in p["sources"]:
        if s["type"] == "xarxanet":
            s["enabled"] = True


def _bad_column(p):
    p["export"]["sheets"][0]["columns"][0]["value"] = "title"


rejects("weights that do not sum to 1.0", _break_weights)
rejects("duplicate dimension keys", _dupe_dimension)
rejects("maybe_above above yes_above", _bad_thresholds)
rejects("a source enabled with no configuration", _enabled_but_empty)
rejects("an unprefixed export column value", _bad_column)

# ===========================================================================
print()
print("=" * 78)
if FAILURES:
    print(f"FAILED — {len(FAILURES)} of {CHECKS} checks:")
    for f in FAILURES:
        print(f"  - {f}")
    print("=" * 78)
    sys.exit(1)
print(f"PASSED — {CHECKS} checks, nothing lost from either notebook.")
print("=" * 78)
