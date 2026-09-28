"""Broad keyword/job-title suggestions, generated from a CV alone.

Deliberately independent of UserProfile: this has to work before any scoring
profile exists (onboarding's "explore broadly, let Claude propose" branch),
and the same call regenerates suggestions later from an existing profile
without any special-casing - see api.py's POST /suggest-keywords.

Mirrors the conventions of prompts.py (system/user split, strict JSON output)
and scoring.py (fence-stripping, retry-on-unusable-reply), applied to a
different, single-shot task: understanding one CV well enough to propose
search terms, not scoring a posting against it.
"""

from __future__ import annotations

import json
import re

SUGGESTION_MODEL = "claude-sonnet-4-6"
SUGGESTION_MAX_TOKENS = 500
SUGGESTION_MAX_ATTEMPTS = 3
MIN_SUGGESTIONS = 3
MAX_SUGGESTIONS = 5

LANGUAGE_NAMES = {"en": "English", "es": "Spanish", "fr": "French", "ca": "Catalan"}


class ResponseError(ValueError):
    """The model's reply could not be used."""


def build_keyword_suggestion_system(language: str | None) -> str:
    """System prompt. States the task, the output language, and the shape."""
    lang_line = (
        f"Write every suggestion in {LANGUAGE_NAMES.get(language, language)}."
        if language else
        "Write every suggestion in the same language as the CV itself."
    )
    return (
        "You read one candidate's CV and propose broad job-title/keyword "
        "search terms for LinkedIn and similar job boards - not a narrow, "
        "hyper-specific title, but terms broad enough to surface a healthy "
        "volume of relevant postings for someone exploring widely from their "
        "CV rather than hunting one exact role.\n"
        f"{lang_line}\n"
        "Each suggestion is a short search phrase (2-5 words) - the kind "
        "someone types into a job board's keyword field. Never a full "
        "sentence, never a qualifier list, never a company name.\n"
        "Capitalize each suggestion the way a real job title is "
        "conventionally capitalized IN ITS OWN LANGUAGE: capitalize the "
        "meaningful words (nouns, adjectives, key verbs), but keep short "
        "connecting words - prepositions, articles, conjunctions - in "
        "lowercase unless one starts the phrase. English: \"AI Engineer\". "
        "Spanish: \"Automatización de Procesos\", NOT \"Automatización De "
        "Procesos\" - do not apply English capitalization rules to another "
        "language. Never write a suggestion fully lowercase or fully "
        "capitalized, and stay consistent across all of them.\n"
        "Return ONLY one valid JSON object. No markdown, no code fence, "
        "no preamble."
    )


def build_keyword_suggestion_prompt(cv_text: str) -> str:
    """The user prompt: just the CV and the task."""
    return (
        "## Candidate CV\n"
        f"{cv_text.strip()}\n\n"
        "## Task\n"
        f"Propose {MIN_SUGGESTIONS} to {MAX_SUGGESTIONS} broad search "
        "keywords/job titles this candidate should search for. Favor "
        "breadth over precision - this candidate is exploring widely from "
        "their CV, not hunting one exact title.\n\n"
        "## Output - this exact JSON shape, nothing else:\n"
        '{\n  "suggestions": ["<broad job title or keyword>", "..."]\n}'
    )


def parse_suggestions(text: str) -> list[str]:
    """Parse the model's JSON and pull out the suggestion list.

    Raises ResponseError on anything unusable, so the caller can retry. An
    empty or missing list is an error, not a silent []: that would read as
    "your CV has nothing to search for", which is never true and would go
    unnoticed until the first real, paid Apify search comes back empty.
    """
    cleaned = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", text.strip())
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ResponseError(f"invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ResponseError(f"expected a JSON object, got {type(payload).__name__}")

    raw = payload.get("suggestions")
    if not isinstance(raw, list) or not raw:
        raise ResponseError(f"expected a non-empty 'suggestions' list, got {raw!r}")

    suggestions = [str(item).strip() for item in raw if str(item).strip()]
    if not suggestions:
        raise ResponseError("every suggestion was empty")
    return suggestions[:MAX_SUGGESTIONS]


def suggest_keywords(
    client,
    cv_text: str,
    *,
    language: str | None = None,
    max_attempts: int = SUGGESTION_MAX_ATTEMPTS,
    model: str = SUGGESTION_MODEL,
) -> list[str]:
    """Propose broad keyword/job-title suggestions from a CV.

    Calls the Anthropic API - this is a billed path, one Sonnet call per
    invocation, not gated by cost_guard.py (which prices Apify specifically;
    this is a single small call, not a per-posting or per-search-URL cost).

    Raises ResponseError on total failure rather than returning an empty
    list, for the same reason parse_suggestions refuses an empty one.
    """
    system = build_keyword_suggestion_system(language)
    prompt = build_keyword_suggestion_prompt(cv_text)

    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=SUGGESTION_MAX_TOKENS,
                system=system,
                messages=[{"role": "user", "content": prompt}],
            )
            return parse_suggestions(response.content[0].text)
        except ResponseError as exc:
            last_error = exc
            if attempt < max_attempts:
                print(f"  [suggest-keywords] unusable reply ({exc}), "
                      f"retry {attempt}/{max_attempts}")
                continue
        except Exception as exc:  # network, rate limit, API error
            last_error = exc
            break

    raise ResponseError(
        f"could not generate keyword suggestions: {last_error}"
    ) from last_error
