"""Application text generation — one mechanism, configured per profile.

Replaces the two separate functions in the notebooks: Gabriel's 550-word
English cover letter and Camila's 150-word Spanish email are the same code
driven by different `WritingConfig` values (ARCHITECTURE.md 6.1).

Every rule that used to be hardcoded in a system prompt is now a field:
required paragraphs, verbatim phrases, banned constructions, the salutation,
the mandatory closing sentence.
"""

from __future__ import annotations

import re

import httpx

from jobscout.profile import UserProfile, WritingConfig
from jobscout.scoring import ScoredJob

DUCKDUCKGO_HTML = "https://html.duckduckgo.com/html/"


def fetch_company_context(company: str, timeout: int = 8) -> str:
    """A one-paragraph description of the company, for personalisation.

    Best effort: scraping a search page is fragile and returns "" on any
    problem. The caller must treat an empty result as normal, not as an error.
    """
    if not company:
        return ""
    try:
        response = httpx.get(
            DUCKDUCKGO_HTML,
            params={"q": f"{company} company about mission product"},
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
            timeout=timeout,
            follow_redirects=True,
        )
        if response.status_code != 200:
            return ""
        snippets = re.findall(
            r'class="result__snippet">(.*?)</a>', response.text, re.DOTALL
        )
        if not snippets:
            return ""
        text = re.sub(r"<[^>]+>", "", snippets[0]).strip()
        return re.sub(r"\s+", " ", text)[:400]
    except Exception:
        return ""


def build_writing_system(profile: UserProfile) -> str:
    """Assemble the system prompt from the profile's writing config."""
    cfg = profile.writing
    if cfg is None:
        raise ValueError(f"profile {profile.profile_id!r} has no writing config")

    kind = "cover letter" if cfg.kind == "cover_letter" else "application email"
    language = {"en": "English", "es": "Spanish", "fr": "French",
                "ca": "Catalan"}.get(cfg.language, cfg.language)

    lines = [
        f"You write a {kind} on behalf of one candidate, in {language}.",
        "",
        "Rules. Follow every one of them:",
    ]
    number = 1

    def rule(text: str) -> None:
        nonlocal number
        lines.append(f"{number}. {text}")
        number += 1

    rule(f"Write in {language} throughout. Never mix languages.")
    rule("First person. Direct, human tone. No filler openers.")

    if cfg.min_words:
        rule(f"Write AT LEAST {cfg.min_words} words. Do not stop short.")
    if cfg.max_words:
        rule(f"Write AT MOST {cfg.max_words} words.")
    if cfg.salutation:
        rule(f'Open with exactly: "{cfg.salutation}" '
             f"(unless the posting names a specific contact).")
    for phrase in cfg.verbatim_phrases:
        rule(f'Use this phrase VERBATIM somewhere in the text: "{phrase}"')
    for paragraph in cfg.required_paragraphs:
        rule(f"Include this paragraph: {paragraph}")
    for banned in cfg.banned:
        rule(f"Never use: {banned}")
    for extra in cfg.extra_rules:
        rule(extra)
    if cfg.closing_sentence:
        rule(f'The final sentence must be EXACTLY: "{cfg.closing_sentence}"')

    rule("Return only the body text. No subject line, no signature, "
         "no commentary about what you wrote.")
    return "\n".join(lines)


def build_writing_prompt(
    profile: UserProfile,
    scored: ScoredJob,
    cv_text: str,
    company_context: str = "",
) -> str:
    """The per-job half of the prompt."""
    cfg = profile.writing
    company = scored.company or "the organisation"
    salutation = (cfg.salutation or "").replace("{company}", company)

    parts = [
        f"Write the {cfg.kind.replace('_', ' ')} for this role.",
        "",
        f"Role: {scored.title}",
        f"Organisation: {company}",
        f"Seen on: {(scored.source or '').capitalize()}",
        f"Summary of the fit: {scored.one_liner}",
    ]
    if scored.match_signals:
        parts.append(f"Matching points: {', '.join(scored.match_signals[:5])}")
    angle = scored.extra.get("cover_letter_angle")
    if angle:
        parts.append(f"Angle to lead with: {angle}")
    patches = scored.extra.get("cv_patches")
    if patches:
        parts.append(f"What to emphasise: {patches}")
    if cfg.include_company_context:
        parts.append(
            f"Company context: {company_context or 'not found, use what you know'}"
        )

    parts += ["", "Candidate profile:", profile.candidate_summary, cv_text.strip()]

    if salutation:
        parts.append(f"\nOpen with: {salutation}")
    return "\n".join(parts)


def should_generate(profile: UserProfile, scored: ScoredJob) -> bool:
    cfg = profile.writing
    return bool(cfg and cfg.enabled and scored.verdict in cfg.generate_for_verdicts)


def generate_text(
    client,
    profile: UserProfile,
    scored: ScoredJob,
    cv_text: str,
    *,
    max_tokens: int = 1600,
) -> str:
    """Generate one application text. Calls the API - this is a billed path.

    Returns "" on failure rather than raising: one failed letter must not lose
    the scoring work already paid for in the same run.
    """
    cfg = profile.writing
    context = ""
    if cfg.include_company_context:
        context = fetch_company_context(scored.company)

    try:
        response = client.messages.create(
            model=cfg.model,
            max_tokens=max_tokens,
            system=build_writing_system(profile),
            messages=[{"role": "user", "content": build_writing_prompt(
                profile, scored, cv_text, context)}],
        )
        return response.content[0].text.strip()
    except Exception as exc:
        print(f"  [write] failed for {scored.title[:40]!r}: "
              f"{type(exc).__name__}: {exc}")
        return ""
