"""Scoring dimensions and rédhibitoire flags, generated from a CV alone.

Mirrors keyword_suggestions.py exactly - same independence from UserProfile
(works before any scoring profile exists, and again later to regenerate),
same "propose, never apply" contract, same fence-stripping/retry discipline.

The one rule that shapes the prompt design, carried over from prompts.py's
own stated principle: **the model is only ever asked for observations,
never for arithmetic.** It is asked for a dimension's IMPORTANCE (1-5) and a
flag's SEVERITY (a qualitative label), never for a weight or an adjustment
number directly - those are computed in Python, so weights are guaranteed to
sum to exactly 1.0 and severities are guaranteed comparable across users
regardless of what the model would have separately called "a -2".

Deliberately out of scope: ExclusionRule and KeywordPenalty (the schema's two
keyword-driven hard filters). Both need an exact keyword list and a field
choice (title/company/location), authored well only when the candidate
states the exact words themselves - an AI reading a free-text CV cannot
reliably invent that list without risking a silent, invisible over-exclusion
of postings that would have been a good match. Only ScoringDimension
(a matter of degree) and LlmFlag (a binary the grader judges by reading the
full posting) are generated here.
"""

from __future__ import annotations

import json
import re

from jobscout.profile import LlmFlag, ScoringDimension

SUGGESTION_MODEL = "claude-sonnet-4-6"
SUGGESTION_MAX_TOKENS = 1500
SUGGESTION_MAX_ATTEMPTS = 3

MIN_DIMENSIONS = 2
MAX_DIMENSIONS = 6
MAX_FLAGS = 5

MIN_IMPORTANCE = 1
MAX_IMPORTANCE = 5

# Fixed magnitudes, not invented per call: a "dealbreaker" means the same
# thing across every candidate's profile, never whatever number one
# particular model call happened to pick. Roughly matches the range already
# used in Camila's hand-built profile (-3.0, -2.5, -1.5, -3.0, +1.0).
SEVERITY_ADJUSTMENTS = {
    "dealbreaker": -5.0,
    "strong_penalty": -3.0,
    "mild_penalty": -1.5,
    "bonus": 1.5,
}

LANGUAGE_NAMES = {"en": "English", "es": "Spanish", "fr": "French", "ca": "Catalan"}

KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")

EXAMPLE_RUBRIC = (
    "- 9-10: Barcelona on-site or hybrid explicitly stated\n"
    "- 7-8: Remote Europe with no geographic exclusion\n"
    "- 5-6: Remote worldwide\n"
    "- 3-4: Remote but excludes this candidate's region\n"
    "- 0-2: Requires relocation the candidate cannot make"
)


class ResponseError(ValueError):
    """The model's reply could not be used."""


def build_scoring_criteria_system(language: str | None) -> str:
    """System prompt. States the task, the two kinds of criteria, and bans
    the model from doing the arithmetic itself."""
    lang_line = (
        f"Write every label, rubric, definition and note in "
        f"{LANGUAGE_NAMES.get(language, language)}."
        if language else
        "Write every label, rubric, definition and note in the same "
        "language as the CV itself."
    )
    return (
        "You read one candidate's CV (and optionally the broad search "
        "keywords they already chose) and propose the scoring rubric a "
        "job-fit assessor will later use to score postings for this "
        "candidate - never the postings themselves, just the criteria.\n"
        "Propose exactly two kinds of criteria, and never blur them:\n"
        "- DIMENSIONS: anything that is a matter of DEGREE, scored 0-10 on a "
        "spectrum (how technical the role is, how senior, how close the "
        "location is, how relevant the sector). Propose 3 to 5.\n"
        "- FLAGS: anything that is a BINARY dealbreaker or bonus, never a "
        "spectrum - a hard requirement or absolute exclusion the candidate "
        "would state themselves (e.g. \"never a posting requiring Catalan\", "
        "\"only permanent contracts\"). Propose 0 to 3 - it is normal for a "
        "candidate to have none.\n"
        "Each dimension needs a 5-band rubric in exactly this shape, one "
        "band per line, calibrated so most real postings land in the "
        "middle:\n"
        f"{EXAMPLE_RUBRIC}\n"
        "For each dimension, also give its IMPORTANCE relative to the "
        "others, as an integer from 1 (least important) to 5 (most "
        "important) - never a weight or a percentage, that arithmetic is "
        "done elsewhere.\n"
        "For each flag, also give its SEVERITY - exactly one of "
        "\"dealbreaker\", \"strong_penalty\", \"mild_penalty\", \"bonus\" - "
        "never a numeric adjustment, that too is decided elsewhere.\n"
        "Every key is a short snake_case identifier (lowercase letters, "
        "digits, underscores, starting with a letter), unique within its "
        "own list.\n"
        f"{lang_line}\n"
        "Return ONLY one valid JSON object. No markdown, no code fence, "
        "no preamble."
    )


def build_scoring_criteria_prompt(
    cv_text: str, search_keywords: list[str] | None = None
) -> str:
    """The user prompt: the CV, optionally the chosen search keywords, and
    the exact output shape."""
    parts = ["## Candidate CV", cv_text.strip()]

    if search_keywords:
        parts.append("\n## Search keywords this candidate already chose")
        parts.append(", ".join(search_keywords))
        parts.append(
            "Use these to anchor the dimensions on the right field - they "
            "are a strong signal of what role this candidate is actually "
            "targeting."
        )

    parts.append("\n## Output - this exact JSON shape, nothing else:")
    parts.append(json.dumps({
        "dimensions": [{
            "key": "<snake_case>", "label": "<short display label>",
            "importance": "<integer 1-5>", "rubric": "<5-band rubric>",
        }],
        "flags": [{
            "key": "<snake_case>", "definition": "<when the grader should set this true>",
            "severity": "<dealbreaker|strong_penalty|mild_penalty|bonus>",
            "note": "<short phrase shown to the candidate>",
        }],
    }, indent=2, ensure_ascii=False))

    return "\n".join(parts)


def _normalize_importances(importances: list[float]) -> list[float]:
    """Relative importances -> weights summing to EXACTLY 1.0.

    Never left to the model: dividing by the total is trivial for Python and
    unreliable for an LLM to get exact across 3-5 numbers. The residual left
    by rounding is folded into the largest weight, so
    UserProfile._weights_sum_to_one (tolerance 1e-6) always accepts it.
    """
    total = sum(importances)
    weights = [round(imp / total, 4) for imp in importances]
    residual = round(1.0 - sum(weights), 4)
    if residual:
        idx = max(range(len(weights)), key=lambda i: weights[i])
        weights[idx] = round(weights[idx] + residual, 4)
    assert abs(sum(weights) - 1.0) < 1e-6, (
        f"normalization bug: weights {weights} sum to {sum(weights)}, not 1.0"
    )
    return weights


def parse_scoring_criteria(text: str) -> dict:
    """Parse the model's JSON into schema-conformant dimensions and flags.

    Raises ResponseError on anything unusable, so the caller can retry.
    Every dimension and flag is round-tripped through ScoringDimension /
    LlmFlag themselves before being returned, so a reply that LOOKS right but
    would not actually validate against the real schema is caught here, not
    discovered later when Lovable tries to save it.
    """
    cleaned = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", text.strip())
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ResponseError(f"invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ResponseError(f"expected a JSON object, got {type(payload).__name__}")

    raw_dims = payload.get("dimensions")
    if not isinstance(raw_dims, list) or not (MIN_DIMENSIONS <= len(raw_dims) <= MAX_DIMENSIONS):
        raise ResponseError(
            f"expected {MIN_DIMENSIONS}-{MAX_DIMENSIONS} dimensions, got {raw_dims!r}")

    raw_flags = payload.get("flags")
    if raw_flags is None:
        raw_flags = []
    if not isinstance(raw_flags, list) or len(raw_flags) > MAX_FLAGS:
        raise ResponseError(f"expected at most {MAX_FLAGS} flags, got {raw_flags!r}")

    importances: list[float] = []
    dim_specs: list[dict] = []
    for item in raw_dims:
        if not isinstance(item, dict):
            raise ResponseError(f"dimension is not an object: {item!r}")
        key = str(item.get("key", "")).strip()
        if not KEY_PATTERN.match(key):
            raise ResponseError(f"dimension key is not snake_case: {key!r}")
        try:
            importance = int(item.get("importance"))
        except (TypeError, ValueError) as exc:
            raise ResponseError(
                f"dimension {key!r} has a non-integer importance: "
                f"{item.get('importance')!r}"
            ) from exc
        if not (MIN_IMPORTANCE <= importance <= MAX_IMPORTANCE):
            raise ResponseError(
                f"dimension {key!r} importance {importance} out of "
                f"[{MIN_IMPORTANCE}, {MAX_IMPORTANCE}]")
        label = str(item.get("label", "")).strip()
        rubric = str(item.get("rubric", "")).strip()
        if not label or not rubric:
            raise ResponseError(f"dimension {key!r} is missing a label or rubric")
        importances.append(importance)
        dim_specs.append({"key": key, "label": label, "rubric": rubric})

    keys = [d["key"] for d in dim_specs]
    if len(set(keys)) != len(keys):
        raise ResponseError(f"duplicate dimension keys: {keys}")

    weights = _normalize_importances(importances)
    dimensions: list[dict] = []
    for spec, weight in zip(dim_specs, weights):
        candidate = {**spec, "weight": weight}
        try:
            dimensions.append(ScoringDimension(**candidate).model_dump())
        except Exception as exc:
            raise ResponseError(
                f"dimension {spec['key']!r} does not validate: {exc}"
            ) from exc

    flags: list[dict] = []
    flag_keys: list[str] = []
    for item in raw_flags:
        if not isinstance(item, dict):
            raise ResponseError(f"flag is not an object: {item!r}")
        key = str(item.get("key", "")).strip()
        if not KEY_PATTERN.match(key):
            raise ResponseError(f"flag key is not snake_case: {key!r}")
        severity = str(item.get("severity", "")).strip()
        if severity not in SEVERITY_ADJUSTMENTS:
            raise ResponseError(
                f"flag {key!r} has an unknown severity {severity!r}, "
                f"expected one of {sorted(SEVERITY_ADJUSTMENTS)}")
        definition = str(item.get("definition", "")).strip()
        note = str(item.get("note", "")).strip()
        if not definition or not note:
            raise ResponseError(f"flag {key!r} is missing a definition or note")

        adjustment = SEVERITY_ADJUSTMENTS[severity]
        target = "match_signals" if adjustment > 0 else "red_flags"
        flag_keys.append(key)
        try:
            flags.append(LlmFlag(
                key=key, definition=definition, adjustment=adjustment,
                note=note, target=target,
            ).model_dump())
        except Exception as exc:
            raise ResponseError(f"flag {key!r} does not validate: {exc}") from exc

    if len(set(flag_keys)) != len(flag_keys):
        raise ResponseError(f"duplicate flag keys: {flag_keys}")

    return {"dimensions": dimensions, "llm_flags": flags}


def suggest_scoring_criteria(
    client,
    cv_text: str,
    *,
    search_keywords: list[str] | None = None,
    language: str | None = None,
    max_attempts: int = SUGGESTION_MAX_ATTEMPTS,
    model: str = SUGGESTION_MODEL,
) -> dict:
    """Propose scoring dimensions and rédhibitoire flags from a CV.

    Calls the Anthropic API - this is a billed path, one Sonnet call per
    invocation, not gated by cost_guard.py for the same reason
    suggest_keywords isn't: a single small call, not a per-posting or
    per-search-URL cost.

    Raises ResponseError on total failure rather than returning an empty
    result: an empty rubric would silently fall back to the one-dimension
    MVP profile with no explanation, which is exactly the outcome this
    route exists to move a candidate past.
    """
    system = build_scoring_criteria_system(language)
    prompt = build_scoring_criteria_prompt(cv_text, search_keywords)

    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=SUGGESTION_MAX_TOKENS,
                system=system,
                messages=[{"role": "user", "content": prompt}],
            )
            return parse_scoring_criteria(response.content[0].text)
        except ResponseError as exc:
            last_error = exc
            if attempt < max_attempts:
                print(f"  [suggest-scoring-criteria] unusable reply ({exc}), "
                      f"retry {attempt}/{max_attempts}")
                continue
        except Exception as exc:  # network, rate limit, API error
            last_error = exc
            break

    raise ResponseError(
        f"could not generate scoring criteria: {last_error}"
    ) from last_error
