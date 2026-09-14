"""Build example_data/*.json from the real run, for handing to Lovable.

    python3 tools/build_example_data.py

Reads store_data/ only. No network, no API call.

Everything in example_job_results.json is a real row from the run: scores,
sub-scores, flags, penalties and prose are exactly what the engine produced.
The only edit is that `generated_text` is truncated, because the full cover
letters name the candidate and these files are meant to be pasted into a
third-party prompt.

example_applications.json is invented, since no application has been tracked
yet. It is the one file here that is illustrative rather than real.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from jobscout.profile import load_profile  # noqa: E402
from jobscout.store.json_store import JsonStore  # noqa: E402

OUT_DIR = REPO / "example_data"
TEXT_PREVIEW = 200


def pick(rows, profile):
    """Two rows per verdict: the best and the worst of each band.

    Picking the extremes rather than the top N means the sample spans the
    whole score range and shows what a bad match looks like, not just a good one.
    """
    chosen = []
    for verdict in ("YES", "MAYBE", "NO"):
        band = sorted([r for r in rows if r.verdict == verdict],
                      key=lambda r: -r.score)
        if not band:
            continue
        chosen.append(band[0])
        if len(band) > 1:
            chosen.append(band[-1])
    return chosen


def to_example(row, profile) -> dict:
    out = row.model_dump()
    out["verdict_display"] = profile.verdict_labels.label(row.verdict)
    if out["generated_text"]:
        text = out["generated_text"]
        out["generated_text"] = (
            text[:TEXT_PREVIEW].rstrip()
            + f"… [truncated for this sample; the real value is {len(text)} chars]"
        )
    return out


FIELD_GUIDE = {
    "_what_this_is": (
        "Real rows from the job_results table, produced by a real run on "
        "2026-09-13. This is exactly what the frontend reads to show a user "
        "their offers. One row = one scored job posting."
    ),
    "user_id": (
        "Owner of the row. In this sample it is the profile name because the "
        "run used the local JSON store. In Supabase it is a uuid referencing "
        "auth.users(id), and row-level security restricts every row to it."
    ),
    "run_id": "Which execution produced this row. Several runs accumulate.",
    "job_key": (
        "Stable identity of the posting: md5(normalised title + company + "
        "tracking-stripped url). Used to avoid re-scoring the same job."
    ),
    "url": (
        "The posting itself. UNIQUE per user: the same offer is never stored "
        "or billed twice, whichever run found it."
    ),
    "source": "Which connector found it: weworkremotely.com, xarxanet, infojobs, linkedin…",
    "verdict": (
        "One of exactly YES / MAYBE / NO. Always these three internal values, "
        "never a translated label — sort and filter on this."
    ),
    "verdict_display": (
        "The same verdict in the user's own language, for display only. "
        "Gabriel sees OUI/OPPORTUNISTE/NON, Camila sees SÍ/OPORTUNISTA/NO. "
        "NOT a column of the table: computed from the profile. Shown here so "
        "the UI knows the distinction exists."
    ),
    "score": (
        "Final score 0-10, one decimal. This is base_score after every bonus "
        "and penalty. It is what the verdict is derived from."
    ),
    "base_score": (
        "Weighted average of the sub-scores, BEFORE adjustments. When it "
        "differs from `score`, an adjustment fired — see red_flags and flags. "
        "Showing both lets a user see why an offer was downgraded."
    ),
    "breakdown": (
        "Sub-score 0-10 per scoring dimension of the user's profile. The keys "
        "differ per user: Gabriel has tech_fit/location/company_size/seniority, "
        "Camila has perfil_fit/location/entidad_fit/condiciones. Do NOT hardcode "
        "the keys in the UI — read them from the row."
    ),
    "flags": (
        "Booleans the model was asked for, defined by the user's profile. "
        "Gabriel declares none, so his are always empty. Camila declares five "
        "(catalan_imprescindible, homologacion_requerida, …). Each true flag "
        "applies a fixed score adjustment."
    ),
    "one_liner": "One sentence naming the decisive factor. The headline of a result card.",
    "match_signals": "Concrete matches found in the posting. Entries starting with [+N.N] are bonuses that were applied.",
    "gaps": "Concrete gaps between the candidate and the posting.",
    "red_flags": "Blockers. Entries starting with [-N.N] are penalties that were applied to the score.",
    "extra": (
        "Profile-declared extra output fields. Gabriel asks for cv_patches and "
        "cover_letter_angle, Camila for tipo_contrato and notes. Varies per user."
    ),
    "generated_text": (
        "The cover letter or application email, when one was generated. Only "
        "produced for the verdicts the profile asks for. TRUNCATED in this sample."
    ),
    "salary_range_market": "Market range for this role and location, in the user's language.",
    "evaluated_at": "When the model scored it.",
    "_verdict_thresholds": (
        "Both users currently use: YES if score > 7.0, MAYBE if score >= 5.0, "
        "else NO. These live in the user's profile and are per user — do not "
        "hardcode them in the UI."
    ),
}

APPLICATION_GUIDE = {
    "_what_this_is": (
        "ILLUSTRATIVE, not real: no application has been tracked yet. Shows "
        "the shape of the applications table. It is deliberately SEPARATE from "
        "job_results so that a new run can add results without ever "
        "overwriting what the user typed here."
    ),
    "user_id": "Same owner as the job_result. uuid in Supabase.",
    "job_url": (
        "Which job_result this tracks. In Postgres the table stores "
        "job_result_id (a uuid foreign key with ON DELETE CASCADE); the URL is "
        "shown here because it is what identifies the row for a human."
    ),
    "status": (
        "The user's own progress. Suggested values: to_apply, applied, "
        "interview, rejected, accepted. Not constrained by the database - "
        "pick the set the UI needs."
    ),
    "priority": (
        "Defaults to a computed value (high if score >= 8.5 or published "
        "within a day; medium if >= 7.5 or within 3 days; else low), rendered "
        "with the user's own labels. Once the user sets it here, theirs wins."
    ),
    "target_salary": "What the user is aiming for. Free text.",
    "notes": "Free text belonging to the user. Never touched by a run.",
    "updated_at": "Maintained by a database trigger.",
}


def main() -> int:
    OUT_DIR.mkdir(exist_ok=True)
    store = JsonStore(REPO / "store_data")

    rows, per_profile = [], {}
    for name in ("gabriel", "camila"):
        profile = load_profile(REPO / "profiles" / f"{name}.json", base_dir=REPO)
        chosen = pick(store.get_results(name), profile)
        per_profile[name] = chosen
        rows.extend(to_example(r, profile) for r in chosen)

    results_doc = {
        "_field_guide": FIELD_GUIDE,
        "_sample": {
            "rows": len(rows),
            "users": {n: len(v) for n, v in per_profile.items()},
            "sources_present": sorted({r["source"] for r in rows}),
            "verdicts_present": sorted({r["verdict"] for r in rows}),
            "score_range": [min(r["score"] for r in rows),
                            max(r["score"] for r in rows)],
            "note": (
                "Real data from a run on 2026-09-13. Gabriel's rows are all "
                "from weworkremotely.com because that run capped at 25 "
                "postings and the RSS connector returned them first; his "
                "LinkedIn connector was enabled but its results fell outside "
                "the cap."
            ),
        },
        "job_results": rows,
    }
    (OUT_DIR / "example_job_results.json").write_text(
        json.dumps(results_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # Applications: invented statuses over six of the real URLs above.
    STATUSES = [
        ("gabriel", 0, "applied", "Candidature envoyée le 13/09, lettre générée jointe."),
        ("gabriel", 1, "to_apply", "Bon score mais poste 100% remote US, à vérifier."),
        ("gabriel", 2, "rejected", "Réponse négative reçue, profil trop junior."),
        ("camila", 0, "interview", "Entrevista el martes a las 10h."),
        ("camila", 1, "applied", "Email enviado el 13/09."),
        ("camila", 3, "rejected", "Piden título homologado, aún en trámite."),
    ]
    applications = []
    for user, index, status, notes in STATUSES:
        chosen = per_profile[user]
        if index >= len(chosen):
            continue
        row = chosen[index]
        profile = load_profile(REPO / "profiles" / f"{user}.json", base_dir=REPO)
        from jobscout.export.computed import priority_key
        applications.append({
            "user_id": user,
            "job_url": row.url,
            "job_title": row.title,          # not a column; here for readability
            "status": status,
            "priority": profile.export.priority_labels[
                priority_key(row.score, row.published)],
            "target_salary": ("€55,000-€65,000" if user == "gabriel"
                              else "18.000-22.000 EUR/año"),
            "notes": notes,
            "updated_at": "2026-09-13T18:00:00",
        })

    applications_doc = {
        "_field_guide": APPLICATION_GUIDE,
        "_sample": {
            "rows": len(applications),
            "statuses_used": sorted({a["status"] for a in applications}),
            "note": ("Statuses and notes are invented. The job_url and "
                     "job_title of each row are real and match a row in "
                     "example_job_results.json."),
        },
        "applications": applications,
    }
    (OUT_DIR / "example_applications.json").write_text(
        json.dumps(applications_doc, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")

    print(f"example_data/example_job_results.json   {len(rows)} rows")
    print(f"example_data/example_applications.json  {len(applications)} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
