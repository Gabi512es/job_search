"""Adapter: turns the two original notebooks into profiles/*.json.

Run it to regenerate the profiles:

    python3 tools/build_profiles.py

Design rule: every value that exists in the notebooks is READ from them, never
retyped here. Only genuinely new, presentational material (column labels,
writing-config structure) appears as literals below, and each such block says
so. That is what makes the conversion auditable — and what lets
tests/test_profile_completeness.py prove nothing was dropped.

Output goes to profiles/, which is gitignored: these files contain real
personal data.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tests.legacy_values import (  # noqa: E402
    CAMILA_NB,
    GABRIEL_NB,
    cell_sources,
    find_flag_adjustments,
    find_list_literal,
    find_thresholds,
    find_weights,
)

DEAD_FEEDS = {
    # Measured 2026-09-09: /feed 301-redirects to the HTML homepage.
    "https://www.arbeitnow.com/feed",
    # Measured 2026-09-09: 404.
    "https://remotive.com/remote-jobs/software-dev/feed",
}


# ---------------------------------------------------------------------------
# Extractors for prompt prose
# ---------------------------------------------------------------------------

def gabriel_rubrics(prompt: str) -> dict[str, str]:
    """Pull the four '### N. Title (weight X%)' bands out of Gabriel's prompt."""
    blocks = re.findall(r"### \d\. (.+?)\n(.*?)(?=\n### |\n## )", prompt, re.S)
    # Prompt section order matches the weighting formula order.
    keys = list(find_weights(prompt))
    assert len(blocks) == len(keys) == 4, f"expected 4 rubrics, got {len(blocks)}"
    return {k: body.strip() for k, (_title, body) in zip(keys, blocks)}


def camila_rubrics(prompt: str) -> dict[str, tuple[str, str]]:
    """Pull the four '**key** — question' bands out of Camila's prompt.

    Returns key -> (question, bands).
    """
    blocks = re.findall(
        r"\*\*(\w+)\*\* — (.+?)\n(.*?)(?=\n\*\*\w+\*\* — |\n## )", prompt, re.S
    )
    assert len(blocks) == 4, f"expected 4 rubrics, got {len(blocks)}"
    return {key: (q.strip(), body.strip()) for key, q, body in blocks}


def camila_flag_definitions(prompt: str) -> dict[str, str]:
    """Pull the '## Flags' definitions out of Camila's prompt."""
    section = prompt.split("## Flags (responder con true/false)", 1)[1]
    section = section.split("## Calibración", 1)[0]
    found = re.findall(
        r"^(\w+): (true .*?)(?=\n\w+: |\Z)", section.strip(), re.S | re.M
    )
    out = {k: re.sub(r"\s+", " ", v).strip() for k, v in found}
    assert len(out) == 5, f"expected 5 flag definitions, got {len(out)}: {list(out)}"
    return out


def camila_flag_notes(post_processing: str) -> dict[str, str]:
    """Pull each flag's human-readable note out of the post-processing block.

    The notes are the strings appended to red_flags / match_signals, e.g.
        '[penalty -3.0] català imprescindible'
    """
    notes: dict[str, str] = {}
    # Each `if result...('key')` block is followed within a few lines by the
    # note string it appends.
    for m in re.finditer(
        r"if\s+result(?:\.get\(|\[)'(?P<key>\w+)'\)?\]?\s*:(?P<body>.*?)"
        r"(?=\n\s{8}(?:if|#|result|for)\b|\Z)",
        post_processing,
        re.S,
    ):
        body = m.group("body")
        note = re.search(r"\[(?:penalty [^\]]+|\+[\d.]+)\]\s*([^']+)'", body)
        if note:
            notes[m.group("key")] = re.sub(r"\s+", " ", note.group(1)).strip()
    return notes


# ---------------------------------------------------------------------------
# Gabriel
# ---------------------------------------------------------------------------

def build_gabriel() -> dict:
    cells = cell_sources(GABRIEL_NB)
    prompt = cells[10]

    weights = find_weights(prompt)
    rubrics = gabriel_rubrics(prompt)
    thresholds = find_thresholds(prompt)
    feeds = [f for f in find_list_literal(cells[5], "RSS_FEEDS") if f not in DEAD_FEEDS]

    # NEW (presentational only): short labels for the score breakdown, taken
    # from the notebook's own print format "Tech:.. Loc:.. Size:.. Sen:..".
    labels = {"tech_fit": "Tech", "location": "Loc",
              "company_size": "Size", "seniority": "Sen"}

    return {
        "profile_id": "gabriel",
        "display_name": "Gabriel",
        "language": "en",
        "cv_path": "profiles/cv/gabriel.txt",
        "candidate_summary": (
            "AI Automation & Workflow Engineer, 1.5y XP, Barcelona. "
            "Open to hybrid or on-site in Barcelona, or remote EU."
        ),
        "dimensions": [
            {"key": k, "label": labels[k], "weight": weights[k], "rubric": rubrics[k]}
            for k in weights
        ],
        "thresholds": thresholds,
        # Preserves the notebook's French display values.
        "verdict_labels": {"yes": "OUI", "maybe": "OPPORTUNISTE", "no": "NON"},
        "llm_flags": [],
        "keyword_penalties": [],
        "exclusions": [
            {
                "kind": "any_keyword",
                # Gabriel's is_excluded searches title + company only.
                "fields": ["title", "company"],
                "keywords": find_list_literal(cells[5], "EXCLUDED_KEYWORDS"),
            }
        ],
        "extra_output_fields": ["cv_patches", "cover_letter_angle"],
        # Narrow by design — the candidate's own salary expectation.
        "target_salary_rule": {
            "junior_range": "€42,000-€45,000",
            "default_range": "€55,000-€65,000",
            "junior_title_words": ["junior", "associate", "entry", "grad", "graduate"],
            "junior_if_seniority_at_least": 8,
        },
        "sources": [
            {"type": "rss", "enabled": True, "feeds": feeds},
            {
                "type": "linkedin_apify",
                "enabled": True,
                "keywords": find_list_literal(cells[6], "LINKEDIN_KEYWORDS"),
                "location": "Barcelona",
                "work_types": ["hybrid", "onsite"],
                "max_per_search": 100,
                "posted_within_days": 7,
            },
            {"type": "infojobs_apify", "enabled": False},
            {"type": "xarxanet", "enabled": False},
        ],
        # NEW structure, existing content: the 10 mandatory rules of
        # COVER_LETTER_SYSTEM, expressed as WritingConfig fields.
        "writing": {
            "kind": "cover_letter",
            "enabled": True,
            "model": "claude-sonnet-4-6",
            "language": "en",
            "min_words": 550,
            "salutation": "Dear Hiring Team,",
            "closing_sentence": "I would love to discuss the role in more detail.",
            "required_paragraphs": [
                "A paragraph starting with \"One small aside:\" explaining that "
                "Gabriel built an automated job-scouting pipeline that surfaced "
                "this role, screening hundreds of postings using the Claude API."
            ],
            "verbatim_phrases": [
                "generates over 200 templated emails in a single automated run, "
                "eliminating work that previously required hours of manual preparation"
            ],
            "banned": [
                "em dashes (—): use commas, colons, or separate sentences instead",
                "GitHub links, or any offer to share a repository",
                "filler openers (\"I am excited to...\", \"I would be thrilled...\")",
            ],
            "include_company_context": True,
            "generate_for_verdicts": ["YES"],
            "extra_rules": [
                "First person throughout. Direct, assertive, human tone.",
                "Always list the four Anthropic certifications by name: "
                "AI Fluency, Claude 101, Claude Cowork, and Claude Code.",
                "Structure: hook paragraph, technical proof paragraph "
                "(Amazon impact + freelance projects + certs), why-this-company "
                "paragraph, \"One small aside\" paragraph, brief close.",
                "Use the salutation unless the job description names a specific contact.",
            ],
        },
        "export": {
            "sheets": _gabriel_sheets(),
            # _tracker_priority in the notebook returns these emoji.
            "priority_labels": {"high": "\U0001F534", "medium": "\U0001F7E0",
                                "low": "\U0001F7E1"},
            "default_application_status": "\u00c0 postuler",
        },
    }


def _gabriel_sheets() -> list[dict]:
    """NEW structure, existing content: the notebook's COLUMNS and
    TRACKER_COLS_DEF, as data. Labels and widths are copied verbatim."""
    return [
        {
            "name": "Job Scout",
            "columns": [
                {"label": "Run date", "width": 18, "value": "computed:run_date"},
                {"label": "Verdict", "width": 12, "value": "computed:verdict_display"},
                {"label": "Score", "width": 7, "value": "field:score"},
                {"label": "Titre", "width": 35, "value": "field:title"},
                {"label": "Entreprise", "width": 22, "value": "field:company"},
                {"label": "Source", "width": 14, "value": "field:source"},
                {"label": "Publié", "width": 14, "value": "computed:published_date"},
                {"label": "One-liner", "width": 55, "value": "field:one_liner"},
                {"label": "Match signals", "width": 35, "value": "field:match_signals"},
                {"label": "Gaps", "width": 35, "value": "field:gaps"},
                {"label": "CV patches", "width": 45, "value": "field:cv_patches"},
                {"label": "Cover letter", "width": 80, "value": "field:generated_text"},
                {"label": "Angle cover", "width": 45, "value": "field:cover_letter_angle"},
                {"label": "Red flags", "width": 35, "value": "field:red_flags"},
                {"label": "Score breakdown", "width": 32, "value": "computed:score_breakdown"},
                {"label": "URL", "width": 55, "value": "field:url"},
            ],
        },
        {
            "name": "Tracker",
            "only_verdicts": ["YES"],
            "columns": [
                {"label": "Company", "width": 22, "value": "field:company"},
                {"label": "Role", "width": 38, "value": "field:title"},
                {"label": "Salary Range (Market)", "width": 24, "value": "field:salary_range_market"},
                {"label": "Target Salary", "width": 16, "value": "computed:target_salary"},
                {"label": "Status", "width": 14, "value": "computed:application_status"},
                {"label": "Application Link", "width": 48, "value": "field:url"},
                {"label": "Priority", "width": 12, "value": "computed:priority"},
                {"label": "Notes", "width": 55, "value": "field:one_liner"},
                {"label": "Cover Letter", "width": 80, "value": "field:generated_text"},
            ],
        },
    ]


# ---------------------------------------------------------------------------
# Camila
# ---------------------------------------------------------------------------

def build_camila() -> dict:
    cells = cell_sources(CAMILA_NB)
    prompt = cells[10]
    post = cells[12]

    weights = find_weights(prompt)
    rubrics = camila_rubrics(prompt)
    thresholds = find_thresholds(prompt)
    adjustments = find_flag_adjustments(post)
    definitions = camila_flag_definitions(prompt)
    notes = camila_flag_notes(post)

    labels = {"perfil_fit": "Perfil", "location": "Loc",
              "entidad_fit": "Entidad", "condiciones": "Cond"}

    # The five flags the model is asked for. corredor_maresme_r1 is excluded
    # here on purpose: the notebook computes it in Python, so it becomes a
    # keyword_penalty below, not an llm_flag.
    llm_flags = []
    for key, definition in definitions.items():
        llm_flags.append({
            "key": key,
            "definition": definition,
            "adjustment": adjustments[key],
            "note": notes[key],
            "target": "match_signals" if adjustments[key] > 0 else "red_flags",
        })

    return {
        "profile_id": "camila",
        "display_name": "Camila",
        "language": "es",
        "cv_path": "profiles/cv/camila.txt",
        "candidate_summary": (
            "Licenciada en Etnoeducación. Mediación intercultural, educación "
            "social y trabajo comunitario con población vulnerable. Barcelona."
        ),
        "dimensions": [
            {
                "key": k,
                "label": labels[k],
                "weight": weights[k],
                # Keep the prompt's original question line above the bands.
                "rubric": f"{rubrics[k][0]}\n{rubrics[k][1]}",
            }
            for k in weights
        ],
        "thresholds": thresholds,
        "verdict_labels": {"yes": "SÍ", "maybe": "OPORTUNISTA", "no": "NO"},
        "llm_flags": llm_flags,
        "keyword_penalties": [
            {
                "key": "corredor_maresme_r1",
                "keywords": find_list_literal(cells[12], "MARESME_R1_TOWNS"),
                "fields": ["location", "title"],
                "adjustment": adjustments["corredor_maresme_r1"],
                "note": notes["corredor_maresme_r1"],
            }
        ],
        "exclusions": [
            {
                "kind": "any_keyword",
                "fields": ["title", "summary", "company"],
                "keywords": find_list_literal(cells[5], "EXCLUDED_KEYWORDS"),
            },
            {
                "kind": "keyword_unless",
                "fields": ["title", "summary", "company"],
                "keywords": find_list_literal(cells[5], "VOLUNTARIADO_WORDS"),
                "unless_keywords": ["contrato", "contracte"],
            },
            {
                "kind": "location_not_in",
                "fields": ["location"],
                "allow": ["barcelona", "bcn", "remote", "remoto", "remot", "teletrab"],
                "deny": find_list_literal(cells[12], "other_cities"),
            },
        ],
        "extra_output_fields": ["tipo_contrato", "notes"],
        "target_salary_rule": None,
        "sources": [
            {"type": "rss", "enabled": False},
            {"type": "linkedin_apify", "enabled": False},
            {
                "type": "xarxanet",
                "enabled": True,
                "filter_keywords": find_list_literal(cells[5], "XARXANET_FILTER_KEYWORDS"),
            },
            {
                "type": "infojobs_apify",
                "enabled": True,
                "keywords": find_list_literal(cells[5], "SEARCH_KEYWORDS"),
                "province": "Barcelona",
                "max_per_search": 12,
                "filter_keywords": find_list_literal(cells[5], "XARXANET_FILTER_KEYWORDS"),
                "reuse_dump": False,
            },
        ],
        # NEW structure, existing content: the 9 rules of EMAIL_SYSTEM.
        "writing": {
            "kind": "email",
            "enabled": True,
            "model": "claude-sonnet-4-6",
            "language": "es",
            "max_words": 150,
            "salutation": "Hola equipo de {company},",
            "include_company_context": False,
            "generate_for_verdicts": ["YES", "MAYBE"],
            "banned": [
                "mezclar idiomas: siempre en español",
                "otras fórmulas de apertura (Dear, Hi, Estimado/a)",
                "listas con viñetas o formato especial: solo párrafos",
                "asunto, firma o instrucciones: solo el cuerpo del email",
            ],
            "extra_rules": [
                "Menciona el puesto visto en [fuente] de forma natural, en una sola frase.",
                "Incluye 1-2 experiencias o habilidades del CV directamente "
                "relevantes para este puesto. Sé específico/a, no genérico/a.",
                "Tono: directo, motivado, profesional sin ser rígido. Sin frases vacías.",
                "Cierra con una frase corta de disponibilidad para un intercambio "
                "o entrevista.",
            ],
        },
        "export": {
            "sheets": _camila_sheets(),
            "priority_labels": {"high": "Alta", "medium": "Media",
                                "low": "Baja"},
            "default_application_status": "Pendiente",
        },
    }


def _camila_sheets() -> list[dict]:
    """NEW structure, existing content: the notebook's COLUMNS and
    TRACKER_COLS_DEF, labels and widths copied verbatim."""
    return [
        {
            "name": "Job Scout",
            "columns": [
                {"label": "Fecha ejecución", "width": 18, "value": "computed:run_date"},
                {"label": "Veredicto", "width": 12, "value": "computed:verdict_display"},
                {"label": "Puntuación", "width": 9, "value": "field:score"},
                {"label": "Título", "width": 35, "value": "field:title"},
                {"label": "Entidad", "width": 22, "value": "field:company"},
                {"label": "Fuente", "width": 12, "value": "computed:source_label"},
                {"label": "Publicado", "width": 14, "value": "computed:published_date"},
                {"label": "Resumen", "width": 55, "value": "field:one_liner"},
                {"label": "Señales positivas", "width": 35, "value": "field:match_signals"},
                {"label": "Brechas", "width": 35, "value": "field:gaps"},
                {"label": "Email candidatura", "width": 80, "value": "field:generated_text"},
                {"label": "Banderas rojas", "width": 35, "value": "field:red_flags"},
                {"label": "Desglose puntuación", "width": 32, "value": "computed:score_breakdown"},
                {"label": "Enlace", "width": 55, "value": "field:url"},
            ],
        },
        {
            "name": "Tracker",
            "only_verdicts": ["YES"],
            "columns": [
                {"label": "Entidad", "width": 22, "value": "field:company"},
                {"label": "Puesto", "width": 38, "value": "field:title"},
                {"label": "Fuente", "width": 14, "value": "computed:source_label"},
                {"label": "Enlace", "width": 48, "value": "field:url"},
                {"label": "Tipo contrato", "width": 18, "value": "field:tipo_contrato"},
                {"label": "Catalán requerido", "width": 16, "value": "flag:catalan_imprescindible"},
                {"label": "Salario estimado", "width": 22, "value": "field:salary_range_market"},
                {"label": "Prioridad", "width": 12, "value": "computed:priority"},
                {"label": "Estado", "width": 14, "value": "computed:application_status"},
                {"label": "Email candidatura", "width": 80, "value": "field:generated_text"},
                {"label": "Fecha", "width": 14, "value": "computed:run_date"},
            ],
        },
    ]


# ---------------------------------------------------------------------------
# Example profile — fully fictional, the only one tracked in git
# ---------------------------------------------------------------------------

def build_example() -> dict:
    return {
        "profile_id": "example",
        "display_name": "Example Candidate",
        "language": "en",
        "cv_text": "Fictional candidate. Replace with your own CV text or use cv_path.",
        "candidate_summary": "Fictional profile, committed as schema documentation only.",
        "dimensions": [
            {"key": "role_fit", "label": "Role", "weight": 0.6,
             "rubric": "9-10: exact role match\n5-6: adjacent role\n0-2: unrelated"},
            {"key": "location", "label": "Loc", "weight": 0.4,
             "rubric": "9-10: target city\n5-6: remote\n0-2: elsewhere"},
        ],
        "thresholds": {"yes_above": 7.0, "maybe_above": 5.0},
        "verdict_labels": {"yes": "YES", "maybe": "MAYBE", "no": "NO"},
        "llm_flags": [
            {"key": "requires_license", "definition": "true if a mandatory licence is stated",
             "adjustment": -2.0, "note": "licence required", "target": "red_flags"}
        ],
        "keyword_penalties": [
            {"key": "hard_commute", "keywords": ["far town"], "fields": ["location"],
             "adjustment": -1.5, "note": "difficult commute"}
        ],
        "exclusions": [
            {"kind": "any_keyword", "fields": ["title", "company"], "keywords": ["internship"]}
        ],
        "extra_output_fields": [],
        "target_salary_rule": None,
        "sources": [
            {"type": "rss", "enabled": True, "feeds": ["https://hnrss.org/jobs"]},
            {"type": "linkedin_apify", "enabled": False},
            {"type": "infojobs_apify", "enabled": False},
            {"type": "xarxanet", "enabled": False},
        ],
        "writing": None,
        "export": {
            "sheets": [{
                "name": "Results",
                "columns": [
                    {"label": "Verdict", "width": 12, "value": "computed:verdict_display"},
                    {"label": "Score", "width": 7, "value": "field:score"},
                    {"label": "Title", "width": 35, "value": "field:title"},
                    {"label": "URL", "width": 55, "value": "field:url"},
                ],
            }]
        },
    }


def main() -> None:
    out_dir = REPO / "profiles"
    out_dir.mkdir(exist_ok=True)

    for name, builder in (
        ("gabriel", build_gabriel),
        ("camila", build_camila),
        ("example", build_example),
    ):
        data = builder()
        path = out_dir / f"{name}.json"
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"  wrote {path.relative_to(REPO)}  ({path.stat().st_size:,} bytes)")


if __name__ == "__main__":
    print("Building profiles from the original notebooks:")
    main()
