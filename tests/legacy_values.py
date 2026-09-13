"""Extracts the hardcoded values out of the two original notebooks.

The completeness test compares the new JSON profiles against whatever this
module reads back from `job_scout.ipynb` and
`job_scout_camila/job_scout_camila.ipynb`. Reading the real notebooks - rather
than re-typing expected values into the test - is what makes the test a genuine
"nothing was lost" check instead of a restatement of my own transcription.

Notebook source is parsed with `ast`, never executed: these cells call
load_dotenv() and print(), and we only want their literals.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# The ORIGINAL notebooks, frozen under legacy/ at block 6.
#
# Until then these tests read the live notebooks, which were the source of
# truth. Block 6 turned those notebooks into thin launchers, so the reference
# had to be preserved somewhere or every differential test would have silently
# lost the thing it compares against. legacy/ is gitignored: the originals
# still contain the candidate's name inside the cover-letter prompt.
LEGACY = REPO / "legacy"
GABRIEL_NB = LEGACY / "job_scout_original.ipynb"
CAMILA_NB = LEGACY / "job_scout_camila_original.ipynb"

if not GABRIEL_NB.exists():
    raise FileNotFoundError(
        f"{GABRIEL_NB} is missing. The differential tests compare against the "
        f"original notebooks; see legacy/README.md."
    )


def cell_sources(nb_path: Path) -> list[str]:
    """Return every cell's source as a string, indexed like the notebook."""
    nb = json.loads(nb_path.read_text(encoding="utf-8"))
    out = []
    for cell in nb["cells"]:
        src = cell["source"]
        out.append("".join(src) if isinstance(src, list) else src)
    return out


def full_source(nb_path: Path) -> str:
    return "\n".join(cell_sources(nb_path))


def find_list_literal(source: str, name: str) -> list[str]:
    """Find `name = [...]` anywhere in `source`, including inside a function.

    Uses ast so that comments, line wrapping and escaped characters inside the
    list cannot change the result.
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == name:
                return [ast.literal_eval(el) for el in node.value.elts]
    raise AssertionError(f"{name} not found in notebook source")


def find_weights(source: str) -> dict[str, float]:
    """Pull the weighting formula out of the eval prompt.

    Matches e.g. `tech_fit*0.40 + location*0.25 + company_size*0.20`.
    """
    pairs = re.findall(r"(\w+)\s*\*\s*(0\.\d+)", source)
    if not pairs:
        raise AssertionError("no weighting formula found in notebook source")
    return {key: float(w) for key, w in pairs}


def find_thresholds(source: str) -> dict[str, float]:
    """Pull the verdict thresholds out of the prompt text."""
    yes = re.search(r"weighted_score\s*>\s*(\d+\.\d+)", source)
    maybe = re.search(r"(\d+\.\d+)\s*(?:<=|≤)\s*weighted_score", source)
    if not (yes and maybe):
        raise AssertionError("verdict thresholds not found in notebook source")
    return {"yes_above": float(yes.group(1)), "maybe_above": float(maybe.group(1))}


def find_flag_adjustments(source: str) -> dict[str, float]:
    """Pull each flag's numeric adjustment out of the post-processing block.

    Matches both shapes used in the Camila notebook:
        if result.get('catalan_imprescindible'):
            penalized = max(0.0, penalized - 3.0)
        if result['corredor_maresme_r1']:
            penalized = max(0.0, penalized - 3.0)
    """
    pattern = re.compile(
        r"if\s+result(?:\.get\(|\[)'(?P<key>\w+)'\)?\]?\s*:\s*\n"
        r"\s*penalized\s*=\s*(?:max|min)\([\d.]+,\s*penalized\s*"
        r"(?P<sign>[+-])\s*(?P<amount>[\d.]+)\)"
    )
    out: dict[str, float] = {}
    for m in pattern.finditer(source):
        amount = float(m.group("amount"))
        out[m.group("key")] = amount if m.group("sign") == "+" else -amount
    if not out:
        raise AssertionError("no flag adjustments found in notebook source")
    return out


# --------------------------------------------------------------------------
# Reference values, read live from the notebooks
# --------------------------------------------------------------------------

def gabriel() -> dict:
    cells = cell_sources(GABRIEL_NB)
    src = full_source(GABRIEL_NB)
    return {
        "excluded_keywords": find_list_literal(cells[5], "EXCLUDED_KEYWORDS"),
        "rss_feeds": find_list_literal(cells[5], "RSS_FEEDS"),
        "linkedin_keywords": find_list_literal(cells[6], "LINKEDIN_KEYWORDS"),
        "weights": find_weights(cells[10]),
        "thresholds": find_thresholds(cells[10]),
    }


def camila() -> dict:
    cells = cell_sources(CAMILA_NB)
    return {
        "excluded_keywords": find_list_literal(cells[5], "EXCLUDED_KEYWORDS"),
        "voluntariado_words": find_list_literal(cells[5], "VOLUNTARIADO_WORDS"),
        "search_keywords": find_list_literal(cells[5], "SEARCH_KEYWORDS"),
        "xarxanet_filter_keywords": find_list_literal(cells[5], "XARXANET_FILTER_KEYWORDS"),
        "maresme_towns": find_list_literal(cells[12], "MARESME_R1_TOWNS"),
        "other_cities": find_list_literal(cells[12], "other_cities"),
        "weights": find_weights(cells[10]),
        "thresholds": find_thresholds(cells[10]),
        "flag_adjustments": find_flag_adjustments(cells[12]),
    }


if __name__ == "__main__":
    for name, values in (("GABRIEL", gabriel()), ("CAMILA", camila())):
        print(f"\n{'=' * 72}\n{name}\n{'=' * 72}")
        for key, val in values.items():
            if isinstance(val, list):
                print(f"{key} ({len(val)}):")
                print(f"    {val}")
            else:
                print(f"{key}: {val}")
