"""Block 2: proves the new contract + filters behave like the notebooks.

Run it:

    python3 tests/test_filters.py

The core of this file is a DIFFERENTIAL test: the original `is_excluded` and
`deduplicate_jobs` are executed straight out of the notebooks and run against
the same fixed corpus as the new code. Agreement on every job is the pass
condition. Where behaviour changes on purpose, the change is asserted
explicitly and labelled, never glossed over.

No network, no API key, no cost.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import hashlib  # noqa: E402
import json as _json  # noqa: E402
import re  # noqa: E402

from jobscout.filters import (  # noqa: E402
    apply_exclusions,
    deduplicate,
    exclusion_reason,
    filter_new,
    interleave_by_source,
    keyword_filter,
    penalty_applies,
    published_at,
)
from jobscout.jobs import JobPosting, job_key, normalize_text, strip_tracking  # noqa: E402
from jobscout.profile import load_profile  # noqa: E402
from tests.legacy_values import CAMILA_NB, GABRIEL_NB, cell_sources  # noqa: E402

CHECKS = 0
FAILURES: list[str] = []


def check(label: str, actual, expected) -> None:
    global CHECKS
    CHECKS += 1
    if actual == expected:
        print(f"  OK    {label}")
        return
    FAILURES.append(label)
    print(f"  FAIL  {label}")
    print(f"          expected: {expected!r}")
    print(f"          actual  : {actual!r}")


def section(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


# ---------------------------------------------------------------------------
# Load the original functions out of the notebooks (defs only, no side effects)
# ---------------------------------------------------------------------------

def legacy_namespace(nb_path: Path, cell_index: int, **globals_) -> dict:
    """exec one notebook cell and hand back its namespace.

    Only `def` statements and literals run at exec time; the functions resolve
    names like EXCLUDED_KEYWORDS when they are called, which is why those are
    injected here rather than imported.
    """
    ns: dict = {"re": re, "json": _json, "hashlib": hashlib, **globals_}
    exec(cell_sources(nb_path)[cell_index], ns)
    return ns


g_cells = cell_sources(GABRIEL_NB)
c_cells = cell_sources(CAMILA_NB)

# Reuse the extractor from block 1 so the legacy constants are the real ones.
from tests.legacy_values import find_list_literal  # noqa: E402

legacy_g = legacy_namespace(
    GABRIEL_NB, 12,
    EXCLUDED_KEYWORDS=find_list_literal(g_cells[5], "EXCLUDED_KEYWORDS"),
)
legacy_c = legacy_namespace(
    CAMILA_NB, 12,
    EXCLUDED_KEYWORDS=find_list_literal(c_cells[5], "EXCLUDED_KEYWORDS"),
    VOLUNTARIADO_WORDS=find_list_literal(c_cells[5], "VOLUNTARIADO_WORDS"),
)

gabriel = load_profile(REPO / "profiles" / "gabriel.json", base_dir=REPO)
camila = load_profile(REPO / "profiles" / "camila.json", base_dir=REPO)


def as_dict(job: JobPosting) -> dict:
    """The plain-dict shape the notebook functions expect."""
    return {
        "title": job.title, "company": job.company, "url": job.url,
        "summary": job.summary, "source": job.source,
        "published": job.published, "location": job.location,
    }


# ---------------------------------------------------------------------------
# Fixed corpora. Fictional companies on purpose - no real personal data.
# ---------------------------------------------------------------------------

GABRIEL_CORPUS = [
    JobPosting(title="AI Engineer", company="Nova Labs",
               url="https://example.com/1", summary="Python, LLM agents, RAG."),
    JobPosting(title="Data Science Intern", company="Nova Labs",
               url="https://example.com/2", summary="Six month placement."),
    JobPosting(title="Backend Engineer", company="Coding Academy SL",
               url="https://example.com/3", summary="Django and Postgres."),
    JobPosting(title="Machine Learning Engineer", company="Orion AI",
               url="https://example.com/4",
               # 'internship' only in the summary: Gabriel's rule reads
               # title + company only, so this must NOT be excluded.
               summary="We also run an internship programme, but this is a full role."),
    JobPosting(title="Stagiaire développeur", company="Beta SAS",
               url="https://example.com/5", summary="Stage de fin d'études."),
    JobPosting(title="Teaching Assistant", company="Uni Tech",
               url="https://example.com/6", summary="Support lab sessions."),
    JobPosting(title="Senior AI Engineer", company="Vega Systems",
               url="https://example.com/7", summary="Agentic workflows in production."),
]

CAMILA_CORPUS = [
    JobPosting(title="Educador/a social", company="Fundació Alfa",
               url="https://example.org/1", location="Barcelona",
               summary="Contrato indefinido, jornada completa. Trabajo comunitario."),
    JobPosting(title="Mediador/a intercultural", company="Associació Beta",
               url="https://example.org/2", location="Barcelona",
               # voluntariado WITHOUT contrato -> excluded
               summary="Buscamos personas para voluntariado en el barrio."),
    JobPosting(title="Monitor/a de lleure", company="Associació Gamma",
               url="https://example.org/3", location="Barcelona",
               # voluntariado WITH contrato -> kept
               summary="Programa de voluntariado coordinado. Se ofrece contrato temporal."),
    JobPosting(title="Becario/a de integración", company="ONG Delta",
               url="https://example.org/4", location="Barcelona",
               summary="Prácticas formativas."),
    JobPosting(title="Técnico/a de integración social", company="Fundació Epsilon",
               url="https://example.org/5", location="Madrid",
               summary="Contrato estable, acompañamiento a familias."),
    JobPosting(title="Educador/a social", company="Ajuntament Zeta",
               url="https://example.org/6", location="Girona",
               summary="Contrato de un año, proyecto intercultural."),
    JobPosting(title="Acompañamiento a infancia", company="Fundació Eta",
               url="https://example.org/7", location="Teletrabajo",
               summary="Contrato parcial, acompañamiento familias vulnerables."),
    JobPosting(title="Coordinador/a de proyectos", company="Associació Theta",
               url="https://example.org/8", location="",
               summary="Contrato indefinido. Proyecto comunitario intercultural."),
]


# ===========================================================================
section("DIFFERENTIAL — exclusions must match the notebooks exactly")

for label, corpus, profile, legacy_fn in (
    ("gabriel", GABRIEL_CORPUS, gabriel, legacy_g["is_excluded"]),
    ("camila", CAMILA_CORPUS, camila, legacy_c["is_excluded"]),
):
    mismatches = []
    for job in corpus:
        old = bool(legacy_fn(as_dict(job)))
        new = exclusion_reason(job, profile.exclusions) is not None
        if old != new:
            mismatches.append((job.title, job.company, f"notebook={old} new={new}"))
    check(f"{label}: same verdict on all {len(corpus)} jobs", mismatches, [])

# Show what each rule actually caught, so the corpus is visibly exercising them.
_, g_counts = apply_exclusions(GABRIEL_CORPUS, gabriel.exclusions)
_, c_counts = apply_exclusions(CAMILA_CORPUS, camila.exclusions)
print(f"\n  gabriel exclusions by rule: {g_counts}")
print(f"  camila  exclusions by rule: {c_counts}")

check("gabriel drops 4 (intern, coding academy, stagiaire, teaching assistant)",
      sum(g_counts.values()), 4)
check("camila drops 3 (voluntariado-sans-contrato, becario, Madrid)",
      sum(c_counts.values()), 3)
check("camila uses all three rule kinds", sorted(c_counts),
      ["any_keyword", "keyword_unless", "location_not_in"])

# The specific behaviours that are easy to get wrong.
section("EXCLUSION EDGE CASES")

check("'internship' in summary only does NOT exclude for gabriel",
      exclusion_reason(GABRIEL_CORPUS[3], gabriel.exclusions), None)
check("voluntariado WITHOUT contrato is excluded",
      exclusion_reason(CAMILA_CORPUS[1], camila.exclusions), "keyword_unless")
check("voluntariado WITH contrato is kept",
      exclusion_reason(CAMILA_CORPUS[2], camila.exclusions), None)
check("Madrid is excluded", exclusion_reason(CAMILA_CORPUS[4], camila.exclusions),
      "location_not_in")
check("Girona (neither allowed nor denied) is kept",
      exclusion_reason(CAMILA_CORPUS[5], camila.exclusions), None)
check("Teletrabajo is kept", exclusion_reason(CAMILA_CORPUS[6], camila.exclusions), None)
check("empty location is kept, not dropped",
      exclusion_reason(CAMILA_CORPUS[7], camila.exclusions), None)
check("gabriel's rules never look at location",
      exclusion_reason(
          JobPosting(title="AI Engineer", company="Nova Labs", location="Madrid"),
          gabriel.exclusions),
      None)


# ===========================================================================
section("DIFFERENTIAL — deduplication vs the Camila notebook")

DUPES = [
    JobPosting(title="Educador/a social", company="Fundació Alfa",
               url="https://example.org/a", summary="Short."),
    JobPosting(title="EDUCADOR/A SOCIAL", company="Fundació Alfa",
               url="https://example.org/b",
               summary="A much longer description with all the useful detail in it."),
    JobPosting(title="Mediador/a intercultural", company="Associació Beta",
               url="https://example.org/c", summary="Unique."),
    JobPosting(title="Educador/a social", company="Fundació Gamma",
               url="https://example.org/d", summary="Same title, other entity."),
]

old_kept = legacy_c["deduplicate_jobs"]([as_dict(j) for j in DUPES])
new_kept, removed = deduplicate(DUPES)

check("same number kept as the notebook", len(new_kept), len(old_kept))
check("same (company, title) pairs kept",
      sorted((j.company, j.title.lower()) for j in new_kept),
      sorted((j["company"], j["title"].lower()) for j in old_kept))
check("same summaries kept (longest wins)",
      sorted(j.summary for j in new_kept),
      sorted(j["summary"] for j in old_kept))
check("removed count reported", removed, 1)
check("the longest summary survived",
      new_kept[0].summary.startswith("A much longer"), True)
check("different company, same title -> both kept", len(new_kept), 3)
check("order of first appearance preserved",
      [j.company for j in new_kept],
      ["Fundació Alfa", "Associació Beta", "Fundació Gamma"])


# ===========================================================================
section("INTENTIONAL CHANGE — gabriel now dedups across sources too")

# Same job, two sources, two URLs. Gabriel's old key included the URL, so both
# survived and both were scored (and billed). The unified strategy collapses
# them. This is a deliberate change, asserted rather than hidden.
CROSS_SOURCE = [
    JobPosting(title="AI Engineer", company="Nova Labs", source="linkedin.com",
               url="https://linkedin.com/jobs/view/1", summary="Short."),
    JobPosting(title="AI Engineer", company="Nova Labs", source="weworkremotely.com",
               url="https://weworkremotely.com/jobs/1",
               summary="The same role, described at much greater length."),
]
old_g = legacy_g["deduplicate_jobs"]([as_dict(j) for j in CROSS_SOURCE])
new_g, _ = deduplicate(CROSS_SOURCE)
check("notebook kept both copies", len(old_g), 2)
check("new code keeps one", len(new_g), 1)
check("and keeps the richer description",
      new_g[0].source, "weworkremotely.com")


# ===========================================================================
section("URL TRACKING + IDENTITY")

DIRTY = ("https://www.linkedin.com/jobs/view/123"
         "?trackingId=abc%3D&refId=xyz&position=4&utm_source=share")
check("tracking params stripped", strip_tracking(DIRTY),
      "https://www.linkedin.com/jobs/view/123")
check("a clean URL is unchanged", strip_tracking("https://example.com/a?id=7"),
      "https://example.com/a?id=7")
check("real params survive alongside tracking ones",
      strip_tracking("https://example.com/a?id=7&utm_medium=email"),
      "https://example.com/a?id=7")
check("malformed URL does not raise", strip_tracking("not a url"), "not a url")

check("job_key ignores tracking params",
      job_key("AI Engineer", "Nova Labs", DIRTY),
      job_key("AI Engineer", "Nova Labs", "https://www.linkedin.com/jobs/view/123"))
check("job_key ignores case and punctuation in title",
      job_key("AI Engineer", "Nova Labs", "https://example.com/1"),
      job_key("ai  engineer!", "NOVA LABS", "https://example.com/1"))
check("job_key differs for a different job",
      job_key("AI Engineer", "Nova Labs", "https://example.com/1")
      != job_key("AI Engineer", "Vega Systems", "https://example.com/1"), True)
check("job_key is 12 hex chars", len(job_key("a", "b", "c")), 12)
check("JobPosting.key matches job_key",
      JobPosting(title="AI Engineer", company="Nova Labs", url=DIRTY).key,
      job_key("AI Engineer", "Nova Labs", DIRTY))

section("NORMALISATION — the change from Gabriel's _norm")

check("punctuation becomes a space, not nothing",
      normalize_text("Senior/Lead Engineer"), "senior lead engineer")
check("the notebook's version glued the words together",
      re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", "Senior/Lead Engineer".lower())).strip(),
      "seniorlead engineer")
check("whitespace collapsed", normalize_text("  AI   Engineer  "), "ai engineer")
check("empty input is safe", normalize_text(""), "")
check("accents are preserved (\\w is unicode-aware)",
      normalize_text("Educació Social"), "educació social")


# ===========================================================================
section("SEEN-JOBS FILTER + SOURCE KEYWORD FILTER")

seen = {GABRIEL_CORPUS[0].key, GABRIEL_CORPUS[6].key}
fresh, skipped = filter_new(GABRIEL_CORPUS, seen)
check("already-seen jobs are dropped", skipped, 2)
check("the rest survive", len(fresh), len(GABRIEL_CORPUS) - 2)
check("nothing dropped with an empty cache", filter_new(GABRIEL_CORPUS, set())[1], 0)

xarxa = camila.source("xarxanet").filter_keywords
kept_kw, dropped_kw = keyword_filter(CAMILA_CORPUS, xarxa)
check("xarxanet keyword filter keeps the on-topic ones", len(kept_kw) > 0, True)
check("an empty keyword list keeps everything",
      keyword_filter(CAMILA_CORPUS, [])[0], CAMILA_CORPUS)
check("a non-matching keyword list drops everything",
      len(keyword_filter(CAMILA_CORPUS, ["quantum cryptography"])[0]), 0)

section("KEYWORD PENALTIES (used by block 4)")

maresme = camila.keyword_penalties[0]
check("Mataró in location triggers the Maresme penalty",
      penalty_applies(JobPosting(title="Educador social", location="Mataró"), maresme),
      True)
check("Mataró in the title also triggers it (location often empty)",
      penalty_applies(JobPosting(title="Educador social a Mataró", location=""), maresme),
      True)
check("Barcelona does not trigger it",
      penalty_applies(JobPosting(title="Educador social", location="Barcelona"), maresme),
      False)
check("the penalty never reads the summary",
      penalty_applies(
          JobPosting(title="Educador social", location="Barcelona",
                     summary="La entidad también tiene sede en Mataró"), maresme),
      False)

# Behaviour parity with the notebook's is_maresme_r1_corridor(location, title).
legacy_maresme = legacy_c["is_maresme_r1_corridor"]
mism = []
for loc, title in (("Mataró", "Educador"), ("", "Plaça a Calella"),
                   ("Barcelona", "Educador"), ("", ""), ("Premià de Mar", "")):
    job = JobPosting(title=title, location=loc)
    if legacy_maresme(loc, title) != penalty_applies(job, maresme):
        mism.append((loc, title))
check("matches the notebook's is_maresme_r1_corridor", mism, [])


# ===========================================================================
section("MIGRATION NOTE (not a failure)")
print("""  job_key changed, so the existing caches no longer match:
    seen_jobs.json          used md5(url)
    seen_jobs_camila.json   used md5(title + company + url), unnormalised
  Neither can be recomputed from its own contents (they store hashes only), so
  on the first run under the new key every previously-seen job looks new and
  would be re-scored once. Mitigation available at block 5: seed the seen_jobs
  table from the URLs already in the .xlsx files rather than from the caches.
  Flagging it now because it has a real Haiku cost.""")


# ===========================================================================
section("PUBLISHED_AT — the two date formats real sources actually send")

from collections import Counter  # noqa: E402
from datetime import datetime, timezone  # noqa: E402

from jobscout.filters import UNKNOWN_DATE  # noqa: E402


def dated(published: str, source: str = "s") -> JobPosting:
    return JobPosting(title="t", company="c", url=f"https://x/{published}{source}",
                      summary="", source=source, published=published)


check("RFC 2822 from the RSS feeds",
      published_at(dated("Wed, 16 Sep 2026 12:01:09 +0000")),
      datetime(2026, 9, 16, 12, 1, 9, tzinfo=timezone.utc))
check("bare ISO date from the LinkedIn actor",
      published_at(dated("2026-09-06")),
      datetime(2026, 9, 6, tzinfo=timezone.utc))
check("ISO with a time and an offset",
      published_at(dated("2026-09-06T08:30:00+02:00")).astimezone(timezone.utc),
      datetime(2026, 9, 6, 6, 30, tzinfo=timezone.utc))
check("an empty date is unknown, not an error", published_at(dated("")), UNKNOWN_DATE)
check("and so is an unreadable one",
      published_at(dated("last tuesday-ish")), UNKNOWN_DATE)
check("every result is timezone-aware, so they can be compared",
      all(published_at(dated(v)).tzinfo is not None
          for v in ("2026-09-06", "", "junk", "Wed, 16 Sep 2026 12:01:09 +0000")), True)


section("INTERLEAVE_BY_SOURCE — a cap can no longer starve a source")

# The shape measured on a real run: one huge source, one paid source, one small
# source, one nearly empty. Sources are named so that alphabetical order is NOT
# the order they are passed in, which is the property being tested.
def batch(source: str, n: int, start_day: int = 1) -> list[JobPosting]:
    return [dated(f"2026-09-{start_day + i:02d}", source) for i in range(n)]


mixed = batch("zz_rss", 10) + batch("linkedin", 8) + batch("aa_feed", 2)
ordered = interleave_by_source(mixed)

check("nothing is lost", len(ordered), len(mixed))
check("and nothing is invented", {j.url for j in ordered}, {j.url for j in mixed})
check("the first three positions hold all three sources",
      sorted(j.source for j in ordered[:3]), ["aa_feed", "linkedin", "zz_rss"])
check("a cap of 3 therefore scores one of each",
      sorted(j.source for j in ordered[:3]), ["aa_feed", "linkedin", "zz_rss"])
check("the exhausted source stops being drawn from",
      [j.source for j in ordered].count("aa_feed"), 2)
check("and its slots go to the others, nothing is wasted",
      len(ordered), 20)

# The bug this replaces: postings arrived grouped, so a prefix took one source.
grouped = batch("zz_rss", 10) + batch("linkedin", 8)
check("BEFORE: a prefix of 8 was one source only",
      len({j.source for j in grouped[:8]}), 1)
check("AFTER: a prefix of 8 is balanced",
      sorted(Counter(j.source for j in interleave_by_source(grouped)[:8]).values()),
      [4, 4])

# Independence from the order the connectors ran in.
forward = interleave_by_source(batch("aa_feed", 5) + batch("linkedin", 5))
backward = interleave_by_source(batch("linkedin", 5) + batch("aa_feed", 5))
check("the result does not depend on connector call order",
      [j.url for j in forward], [j.url for j in backward])

# Recency within a source.
recency = interleave_by_source([
    dated("2026-09-01", "s"), dated("2026-09-30", "s"), dated("2026-09-15", "s"),
])
check("within a source, newest first",
      [j.published for j in recency], ["2026-09-30", "2026-09-15", "2026-09-01"])

undated = interleave_by_source([
    dated("", "s"), dated("2026-09-30", "s"), dated("junk", "s"),
])
check("undated postings sort last within their source, never dropped",
      [j.published for j in undated][0], "2026-09-30")
check("and they are all still there", len(undated), 3)

# Recency is NOT applied across sources: the weworkremotely trap.
old_source = batch("aa_old", 3, start_day=1)      # 2026-09-01..03
new_source = batch("zz_new", 3, start_day=20)     # 2026-09-20..22
across = interleave_by_source(old_source + new_source)
check("an older source is not buried behind a newer one",
      sorted(j.source for j in across[:2]), ["aa_old", "zz_new"])

# Edge cases.
check("an empty list stays empty", interleave_by_source([]), [])
check("a single source is returned newest-first, unchanged in membership",
      len(interleave_by_source(batch("only", 4))), 4)
check("stable for postings sharing a date, which LinkedIn's mostly do",
      [j.url for j in interleave_by_source(
          [dated("2026-09-06", "s"), dated("2026-09-06", "t")])],
      [dated("2026-09-06", "s").url, dated("2026-09-06", "t").url])


# ===========================================================================
print()
print("=" * 78)
if FAILURES:
    print(f"FAILED — {len(FAILURES)} of {CHECKS} checks:")
    for f in FAILURES:
        print(f"  - {f}")
    print("=" * 78)
    sys.exit(1)
print(f"PASSED — {CHECKS} checks. Filters behave like the notebooks.")
print("=" * 78)
