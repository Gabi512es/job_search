"""The scoring engine. All arithmetic and every threshold live here, once.

Division of labour (ARCHITECTURE.md § 4):

    the model  -> one integer per dimension, one boolean per flag, some prose
    Python     -> weighted average, adjustments, clamping, verdict

The model is never shown a weight or a threshold, so it cannot disagree with
Python about them. In the Camila notebook the thresholds existed in the prompt
text *and* in the post-processing, and the verdict was computed twice.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from pydantic import BaseModel, Field

from jobscout.filters import penalty_applies
from jobscout.jobs import JobPosting
from jobscout.profile import UserProfile, Verdict
from jobscout.prompts import build_eval_prompt, build_eval_system

SCORING_MODEL = "claude-haiku-4-5-20251001"
# 1000 truncated detailed JSON responses mid-object in the original notebook.
EVAL_MAX_TOKENS = 2000
EVAL_MAX_ATTEMPTS = 2


def round1(value: float) -> float:
    """Round half away from zero, to one decimal.

    Python's built-in round() is banker's rounding over binary floats, so
    round(7.85, 1) gives 7.8. A score shown to a user should round the way a
    person expects, and the original pipeline's totals (produced by the model)
    round that way too.
    """
    return float(Decimal(str(value)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


class Adjustment(BaseModel):
    """One applied bonus or penalty, kept for display and for audit."""

    key: str
    amount: float
    note: str
    target: str = "red_flags"
    origin: str = "llm_flag"  # or "keyword_penalty"

    @property
    def label(self) -> str:
        return f"[{self.amount:+.1f}] {self.note}"


class VerdictResult(BaseModel):
    """Everything Python derived from the model's observations."""

    base_score: float           # weighted average, before adjustments
    score: float                # after adjustments, clamped to 0-10
    verdict: Verdict
    sub_scores: dict[str, int]
    flags: dict[str, bool] = Field(default_factory=dict)
    adjustments: list[Adjustment] = Field(default_factory=list)

    @property
    def adjustment_total(self) -> float:
        return round1(sum(a.amount for a in self.adjustments))


class ScoredJob(BaseModel):
    """A scored posting: the job, the verdict, and the model's prose."""

    title: str = ""
    company: str = ""
    url: str = ""
    source: str = ""
    published: str = ""
    location: str = ""

    base_score: float = 0.0
    score: float = 0.0
    verdict: Verdict = "NO"
    sub_scores: dict[str, int] = Field(default_factory=dict)
    flags: dict[str, bool] = Field(default_factory=dict)
    adjustments: list[Adjustment] = Field(default_factory=list)

    one_liner: str = ""
    match_signals: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    red_flags: list[str] = Field(default_factory=list)
    salary_range_market: str = ""
    extra: dict[str, Any] = Field(default_factory=dict)

    generated_text: str = ""      # cover letter / email, filled in block 6
    evaluated_at: str = ""


# ---------------------------------------------------------------------------
# The calculation
# ---------------------------------------------------------------------------

def weighted_base(profile: UserProfile, sub_scores: dict[str, int]) -> float:
    """Weighted average of the sub-scores, using the profile's weights."""
    missing = [d.key for d in profile.dimensions if d.key not in sub_scores]
    if missing:
        raise ValueError(f"missing sub-scores for dimensions: {missing}")
    total = sum(float(sub_scores[d.key]) * d.weight for d in profile.dimensions)
    return round1(total)


def collect_adjustments(
    profile: UserProfile, flags: dict[str, bool], job: JobPosting
) -> list[Adjustment]:
    """Every bonus and penalty that applies to this job.

    Two sources, deliberately separate:
      - llm_flags        the model answered true
      - keyword_penalties matched in Python against the job's own fields, never
                          asked of the model
    """
    applied: list[Adjustment] = []

    for flag in profile.llm_flags:
        if flags.get(flag.key):
            applied.append(Adjustment(
                key=flag.key, amount=flag.adjustment, note=flag.note,
                target=flag.target, origin="llm_flag",
            ))

    for penalty in profile.keyword_penalties:
        if penalty_applies(job, penalty):
            applied.append(Adjustment(
                key=penalty.key, amount=penalty.adjustment, note=penalty.note,
                target="match_signals" if penalty.adjustment > 0 else "red_flags",
                origin="keyword_penalty",
            ))

    return applied


def apply_adjustments(base: float, adjustments: list[Adjustment]) -> float:
    """Apply every bonus and penalty to a base score, clamped once into 0-10.

    Split out from compute_verdict so it can be replayed against the base
    scores recorded in the existing result files, independently of how those
    base scores were arrived at.
    """
    return round1(min(10.0, max(0.0, base + sum(a.amount for a in adjustments))))


def compute_verdict(
    profile: UserProfile,
    sub_scores: dict[str, int],
    flags: dict[str, bool],
    job: JobPosting,
) -> VerdictResult:
    """Turn observations into a score and a verdict. The only place this happens.

    Order of operations:
      1. weighted average of the sub-scores
      2. add every adjustment (penalties are negative, bonuses positive)
      3. clamp once into 0-10
      4. compare against the profile's thresholds

    Summing before clamping is deliberate. The original notebook clamped after
    each individual penalty, which makes the result depend on the order the
    adjustments happen to be declared in: a job driven below 0 by penalties and
    then lifted by a bonus lands somewhere different depending on where the
    bonus sits in the list. Reordering flags in a profile JSON would silently
    change scores.

    Checked against the 61 real rows in the Camila result file: summing first
    reproduces all 61 exactly, and is indistinguishable there from clamping in
    declaration order. Clamping penalties-first reproduces only 60.
    """
    base = weighted_base(profile, sub_scores)
    adjustments = collect_adjustments(profile, flags, job)
    score = apply_adjustments(base, adjustments)

    thresholds = profile.thresholds
    if score > thresholds.yes_above:
        verdict: Verdict = "YES"
    elif score >= thresholds.maybe_above:
        verdict = "MAYBE"
    else:
        verdict = "NO"

    return VerdictResult(
        base_score=base, score=score, verdict=verdict,
        sub_scores=dict(sub_scores), flags=dict(flags), adjustments=adjustments,
    )


# ---------------------------------------------------------------------------
# Model response handling
# ---------------------------------------------------------------------------

class ResponseError(ValueError):
    """The model's reply could not be used."""


def parse_response(text: str, profile: UserProfile) -> tuple[dict[str, int], dict[str, bool], dict]:
    """Parse the model's JSON and pull out sub-scores, flags and prose.

    Raises ResponseError on anything unusable, so the caller can retry. Missing
    sub-scores are an error rather than a silent default: scoring a job against
    a dimension the model never answered would produce a confident wrong number.
    """
    cleaned = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", text.strip())
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ResponseError(f"invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ResponseError(f"expected a JSON object, got {type(payload).__name__}")

    raw_scores = payload.get("sub_scores") or {}
    sub_scores: dict[str, int] = {}
    for dimension in profile.dimensions:
        if dimension.key not in raw_scores:
            raise ResponseError(f"missing sub-score for {dimension.key!r}")
        try:
            value = int(round(float(raw_scores[dimension.key])))
        except (TypeError, ValueError) as exc:
            raise ResponseError(
                f"sub-score for {dimension.key!r} is not a number: "
                f"{raw_scores[dimension.key]!r}"
            ) from exc
        sub_scores[dimension.key] = max(0, min(10, value))

    raw_flags = payload.get("flags") or {}
    flags = {f.key: bool(raw_flags.get(f.key, False)) for f in profile.llm_flags}

    return sub_scores, flags, payload


def _as_list(value) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value if v]
    return [str(value)] if value else []


def build_scored_job(
    profile: UserProfile, job: JobPosting, result: VerdictResult, payload: dict
) -> ScoredJob:
    """Combine the computed verdict with the model's prose into one record."""
    red_flags = _as_list(payload.get("red_flags"))
    match_signals = _as_list(payload.get("match_signals"))

    # Adjustments are surfaced in the lists a user actually reads, exactly as
    # the original pipeline did.
    for adjustment in result.adjustments:
        if adjustment.target == "match_signals":
            match_signals.insert(0, adjustment.label)
        else:
            red_flags.append(adjustment.label)

    return ScoredJob(
        title=job.title, company=job.company, url=job.url, source=job.source,
        published=job.published, location=job.location,
        base_score=result.base_score, score=result.score, verdict=result.verdict,
        sub_scores=result.sub_scores, flags=result.flags,
        adjustments=result.adjustments,
        one_liner=str(payload.get("one_liner", "")),
        match_signals=match_signals,
        gaps=_as_list(payload.get("gaps")),
        red_flags=red_flags,
        salary_range_market=str(payload.get("salary_range_market", "")),
        extra={k: payload.get(k, "") for k in profile.extra_output_fields},
        evaluated_at=datetime.now().isoformat(),
    )


# ---------------------------------------------------------------------------
# The one function that spends money
# ---------------------------------------------------------------------------

def score_job(
    client,
    profile: UserProfile,
    job: JobPosting,
    cv_text: str,
    *,
    max_attempts: int = EVAL_MAX_ATTEMPTS,
    model: str = SCORING_MODEL,
    min_description_chars: int = 50,
) -> ScoredJob | None:
    """Score one posting. Calls the Anthropic API - this is the billed path.

    Returns None when the posting cannot be scored (too little text, or the
    model never produced usable JSON). Returning None rather than a default
    score keeps unscoreable jobs out of the results instead of burying them at
    a made-up 5/10.
    """
    description = job.summary or ""
    if len(description) < min_description_chars:
        return None

    system = build_eval_system(profile)
    prompt = build_eval_prompt(profile, job, cv_text)

    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=EVAL_MAX_TOKENS,
                system=system,
                messages=[{"role": "user", "content": prompt}],
            )
            sub_scores, flags, payload = parse_response(
                response.content[0].text, profile
            )
            result = compute_verdict(profile, sub_scores, flags, job)
            return build_scored_job(profile, job, result, payload)
        except ResponseError as exc:
            last_error = exc
            if attempt < max_attempts:
                print(f"  [score] unusable reply for {job.title[:40]!r} "
                      f"({exc}), retry {attempt}/{max_attempts}")
                continue
        except Exception as exc:  # network, rate limit, API error
            last_error = exc
            break

    print(f"  [score] giving up on {job.title[:40]!r}: {last_error}")
    return None
