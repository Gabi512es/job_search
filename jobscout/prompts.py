"""Builds the evaluation prompt from a profile. No scoring logic lives here.

The rule that shapes this whole module: **the model is only ever asked for
observations, never for arithmetic and never for a decision.**

It returns one integer per dimension and one boolean per flag. It is never told
the weights, never told the verdict thresholds, and never asked to compute a
weighted total. All of that happens in scoring.py, in Python, once.

That removes the duplication found in the audit, where the Camila notebook
stated the thresholds in the prompt text *and* re-applied them in Python after
penalties - two sources of truth for the same numbers, which could silently
disagree.
"""

from __future__ import annotations

import json

from jobscout.jobs import JobPosting
from jobscout.profile import UserProfile

# Text fields the model produces for every profile.
CORE_TEXT_FIELDS = {
    "one_liner": "<one sentence naming the decisive factor>",
    "match_signals": ["<specific match found in the description>"],
    "gaps": ["<concrete gap between the candidate and the posting>"],
    "red_flags": ["<hard blocker or dealbreaker>"],
    "salary_range_market": "<market range for this role and location, e.g. '50k-70k EUR/yr'>",
}

MAX_DESCRIPTION_CHARS = 3000


def build_eval_system(profile: UserProfile) -> str:
    """System prompt. States the job and the calibration, nothing numeric."""
    language = {
        "en": "English", "es": "Spanish", "fr": "French", "ca": "Catalan",
    }.get(profile.language, "English")

    return (
        "You are a rigorous job-fit assessor for one specific candidate.\n"
        "You report observations only: a sub-score for each listed dimension, "
        "and a true/false answer for each listed flag.\n"
        "You do NOT decide whether the candidate should apply, and you do NOT "
        "compute any total. That is done elsewhere.\n"
        "Score honestly and do not inflate. Most real postings land in the 4-7 "
        "band on any given dimension. Reserve 9-10 for genuinely exceptional "
        "matches and 0-2 for clear mismatches.\n"
        f"Write all free-text fields in {language}.\n"
        "Return ONLY one valid JSON object. No markdown, no code fence, no preamble."
    )


def _output_schema(profile: UserProfile) -> str:
    """The exact JSON shape the model must return, built from the profile."""
    schema: dict[str, object] = {
        "sub_scores": {d.key: "<integer 0-10>" for d in profile.dimensions},
    }
    if profile.llm_flags:
        schema["flags"] = {f.key: "<true|false>" for f in profile.llm_flags}
    schema.update(CORE_TEXT_FIELDS)
    for field in profile.extra_output_fields:
        schema[field] = "<string, or empty string if not applicable>"
    return json.dumps(schema, indent=2, ensure_ascii=False)


def build_eval_prompt(
    profile: UserProfile, job: JobPosting, cv_text: str
) -> str:
    """Assemble the user prompt: candidate, posting, rubrics, flags, schema.

    Contains no weight and no threshold, by construction - there is nowhere in
    this function that reads profile.thresholds or dimension.weight.
    """
    parts: list[str] = []

    parts.append("## Candidate")
    if profile.candidate_summary:
        parts.append(profile.candidate_summary)
    parts.append(cv_text.strip())

    parts.append("\n## Job posting")
    parts.append(f"Title: {job.title}")
    parts.append(f"Company: {job.company or 'Unknown'}")
    if job.location:
        parts.append(f"Location: {job.location}")
    description = (job.summary or "")[:MAX_DESCRIPTION_CHARS]
    parts.append(f"Description:\n{description}")

    parts.append(
        "\n## Dimensions - give each an integer from 0 to 10"
    )
    for dimension in profile.dimensions:
        parts.append(f"\n### {dimension.key}")
        parts.append(dimension.rubric)

    if profile.llm_flags:
        parts.append("\n## Flags - answer true or false for each")
        for flag in profile.llm_flags:
            parts.append(f"- {flag.key}: {flag.definition}")

    parts.append(
        "\n## Calibration\n"
        "Judge each dimension independently against its own scale above. "
        "Do not let a strong score on one dimension pull another upward. "
        "When the posting gives you nothing to judge a dimension on, score it "
        "in the middle rather than high."
    )

    parts.append("\n## Output - this exact JSON shape, nothing else:")
    parts.append(_output_schema(profile))

    return "\n".join(parts)
