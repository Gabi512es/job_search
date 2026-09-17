"""User profile schema — everything that was hardcoded per person.

One `UserProfile` fully describes one job search: who the candidate is, how
offers are scored, which sources are active, how text is written, how results
are exported. Nothing about a specific person lives in code any more.

A profile is a document: it round-trips to and from JSON with
`UserProfile.model_validate(...)` / `profile.model_dump()`, which is exactly the
shape of the `profiles.config` jsonb column described in ARCHITECTURE.md § 5.3.

The CV itself is never stored in the profile JSON. `cv_path` points at a file
outside git, for local runs. On the server there is no such file: the profile
and the CV both arrive from Supabase through the get-profile route, and
`profile_from_engine()` at the bottom of this module turns that into a
UserProfile.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any, Literal, Mapping

from pydantic import BaseModel, Field, ValidationError, model_validator

from cost_guard import CostPolicy

# Internal verdict values. Language-neutral on purpose: display labels are a
# per-profile concern (see VerdictLabels), so nothing downstream compares
# against a French or Spanish string.
Verdict = Literal["YES", "MAYBE", "NO"]


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

class ScoringDimension(BaseModel):
    """One weighted axis of the score.

    `rubric` is the 0-10 band description injected verbatim into the eval
    prompt. It deliberately contains no weighting formula and no verdict
    threshold: those live in Python only (ARCHITECTURE.md § 4).
    """

    key: str
    label: str
    weight: float = Field(gt=0.0, le=1.0)
    rubric: str


class LlmFlag(BaseModel):
    """A boolean the model is asked for, plus its deterministic adjustment.

    `definition` tells the model when to set it true. `adjustment` is applied in
    Python afterwards, never by the model.
    """

    key: str
    definition: str
    adjustment: float
    note: str
    target: Literal["red_flags", "match_signals"] = "red_flags"


class KeywordPenalty(BaseModel):
    """An adjustment computed in Python from the job's own fields.

    Never asked of the model. This is how Camila's Maresme R1 commute penalty
    works today, and the mechanism generalises to any keyword list.
    """

    key: str
    keywords: list[str] = Field(min_length=1)
    fields: list[str] = ["location", "title"]
    adjustment: float
    note: str


class ExclusionRule(BaseModel):
    """A hard filter applied before scoring. Three kinds, covering the four
    rules that actually exist across the two original pipelines:

    any_keyword      - drop if any keyword appears in `fields`
                       (Gabriel's internships/bootcamps; Camila's becario)
    keyword_unless   - drop if any keyword appears UNLESS an `unless_keywords`
                       term also appears (Camila's voluntariado-without-contrato)
    location_not_in  - drop if `location` matches none of `allow` but matches
                       one of `deny` (Camila's non-Barcelona cities)
    """

    kind: Literal["any_keyword", "keyword_unless", "location_not_in"]
    fields: list[str] = ["title", "company"]
    keywords: list[str] = []
    unless_keywords: list[str] = []
    allow: list[str] = []
    deny: list[str] = []

    @model_validator(mode="after")
    def _check_kind_fields(self) -> ExclusionRule:
        if self.kind in ("any_keyword", "keyword_unless") and not self.keywords:
            raise ValueError(f"exclusion kind {self.kind!r} requires 'keywords'")
        if self.kind == "keyword_unless" and not self.unless_keywords:
            raise ValueError("exclusion kind 'keyword_unless' requires 'unless_keywords'")
        if self.kind == "location_not_in" and not (self.allow or self.deny):
            raise ValueError("exclusion kind 'location_not_in' requires 'allow' or 'deny'")
        return self


class Thresholds(BaseModel):
    """The ONLY place verdict cutoffs are defined. Previously duplicated
    between the prompt text and the Python post-processing."""

    yes_above: float = 7.0
    maybe_above: float = 5.0

    @model_validator(mode="after")
    def _ordered(self) -> Thresholds:
        if self.maybe_above > self.yes_above:
            raise ValueError(
                f"maybe_above ({self.maybe_above}) cannot exceed "
                f"yes_above ({self.yes_above})"
            )
        return self


class VerdictLabels(BaseModel):
    """How the three internal verdicts are shown to this user."""

    yes: str = "YES"
    maybe: str = "MAYBE"
    no: str = "NO"

    def label(self, verdict: str) -> str:
        return {"YES": self.yes, "MAYBE": self.maybe, "NO": self.no}.get(verdict, verdict)


class TargetSalaryRule(BaseModel):
    """Deliberately narrow: this is the candidate's own salary expectation.

    Not a general-purpose mechanism (ARCHITECTURE.md § 2.3 point 1). Optional;
    profiles that do not want it leave it null.
    """

    junior_range: str
    default_range: str
    junior_title_words: list[str] = []
    junior_if_seniority_at_least: int = 8


# ---------------------------------------------------------------------------
# Sources — discriminated union on `type`
# ---------------------------------------------------------------------------
# Every source-specific field has a default so a disabled source can be
# declared as just {"type": ..., "enabled": false}. A validator then requires
# the real fields only when the source is actually switched on.

class _BaseSource(BaseModel):
    enabled: bool = False

    def _require(self, **fields) -> None:
        if not self.enabled:
            return
        missing = [name for name, value in fields.items() if not value]
        if missing:
            raise ValueError(
                f"source {self.type!r} is enabled but missing: {', '.join(missing)}"
            )


class RssSource(_BaseSource):
    type: Literal["rss"] = "rss"
    feeds: list[str] = []

    @model_validator(mode="after")
    def _check(self) -> RssSource:
        self._require(feeds=self.feeds)
        return self


class LinkedInApifySource(_BaseSource):
    type: Literal["linkedin_apify"] = "linkedin_apify"
    keywords: list[str] = []
    location: str = ""
    work_types: list[Literal["hybrid", "onsite", "remote"]] = ["hybrid", "onsite"]
    max_per_search: int = 100
    posted_within_days: int = 7
    reuse_dump: bool = False  # replay a saved raw dump: zero Apify cost

    @model_validator(mode="after")
    def _check(self) -> LinkedInApifySource:
        self._require(keywords=self.keywords, location=self.location)
        return self


class InfoJobsApifySource(_BaseSource):
    type: Literal["infojobs_apify"] = "infojobs_apify"
    keywords: list[str] = []
    province: str = ""
    max_per_search: int = 12
    filter_keywords: list[str] = []
    reuse_dump: bool = False  # replay a saved raw dump: zero Apify cost

    @model_validator(mode="after")
    def _check(self) -> InfoJobsApifySource:
        self._require(keywords=self.keywords, province=self.province)
        return self


class XarxanetSource(_BaseSource):
    type: Literal["xarxanet"] = "xarxanet"
    filter_keywords: list[str] = []

    @model_validator(mode="after")
    def _check(self) -> XarxanetSource:
        self._require(filter_keywords=self.filter_keywords)
        return self


SourceConfig = Annotated[
    RssSource | LinkedInApifySource | InfoJobsApifySource | XarxanetSource,
    Field(discriminator="type"),
]


# ---------------------------------------------------------------------------
# Text generation — one mechanism, two presets
# ---------------------------------------------------------------------------

class WritingConfig(BaseModel):
    """Replaces the two separate cover-letter / email functions.

    Gabriel's 550-word English letter and Camila's 150-word Spanish email are
    both expressed here (ARCHITECTURE.md § 6.1).
    """

    kind: Literal["cover_letter", "email"]
    enabled: bool = True
    model: str = "claude-sonnet-4-6"
    language: str = "en"
    min_words: int | None = None
    max_words: int | None = None
    salutation: str | None = None
    closing_sentence: str | None = None
    required_paragraphs: list[str] = []
    verbatim_phrases: list[str] = []
    banned: list[str] = []
    include_company_context: bool = False
    generate_for_verdicts: list[Verdict] = ["YES"]
    extra_rules: list[str] = []


# ---------------------------------------------------------------------------
# Export — secondary output. The primary destination for results is the
# `job_results` table (ARCHITECTURE.md § 5.3 and § 8).
# ---------------------------------------------------------------------------

class ExportColumn(BaseModel):
    """`value` is one of:
        field:<name>     - a key on the scored result
        computed:<name>  - a named function in export/computed.py
        flag:<key>       - a boolean from llm_flags, rendered as yes/no
    """

    label: str
    width: int = 20
    value: str

    @model_validator(mode="after")
    def _prefixed(self) -> ExportColumn:
        if not self.value.split(":", 1)[0] in ("field", "computed", "flag"):
            raise ValueError(
                f"column {self.label!r}: value must start with "
                f"'field:', 'computed:' or 'flag:', got {self.value!r}"
            )
        return self


class ExportSheet(BaseModel):
    name: str
    columns: list[ExportColumn] = Field(min_length=1)
    only_verdicts: list[Verdict] = []


class ExportConfig(BaseModel):
    sheets: list[ExportSheet] = Field(min_length=1)
    # Shown in the Priority column. The originals used emoji (Gabriel) and
    # Spanish words (Camila), so this has to be per profile.
    priority_labels: dict[str, str] = {"high": "HIGH", "medium": "MEDIUM", "low": "LOW"}
    # Status written for a job that has no application row yet. Once an
    # application exists, its own status wins (they live in a separate table so
    # that a new run cannot overwrite what the user set - ARCHITECTURE.md 5.3).
    default_application_status: str = "To apply"


# ---------------------------------------------------------------------------
# The profile
# ---------------------------------------------------------------------------

class UserProfile(BaseModel):
    profile_id: str
    display_name: str
    language: Literal["en", "es", "fr", "ca"] = "en"

    # The CV never lives in this document. Exactly one of the two is supplied:
    # cv_path for local development, cv_text once Supabase holds it.
    cv_path: str | None = None
    cv_text: str | None = None
    candidate_summary: str = ""

    dimensions: list[ScoringDimension] = Field(min_length=1)
    thresholds: Thresholds = Thresholds()
    verdict_labels: VerdictLabels = VerdictLabels()
    llm_flags: list[LlmFlag] = []
    keyword_penalties: list[KeywordPenalty] = []
    exclusions: list[ExclusionRule] = []
    extra_output_fields: list[str] = []
    target_salary_rule: TargetSalaryRule | None = None

    sources: list[SourceConfig] = Field(min_length=1)
    writing: WritingConfig | None = None
    export: ExportConfig
    cost_policy: CostPolicy = Field(default_factory=CostPolicy)

    # -- validation ---------------------------------------------------------

    @model_validator(mode="after")
    def _weights_sum_to_one(self) -> UserProfile:
        total = sum(d.weight for d in self.dimensions)
        if abs(total - 1.0) > 1e-6:
            raise ValueError(
                f"dimension weights must sum to 1.0, got {total:.4f} "
                f"({', '.join(f'{d.key}={d.weight}' for d in self.dimensions)})"
            )
        return self

    @model_validator(mode="after")
    def _unique_keys(self) -> UserProfile:
        for name, keys in (
            ("dimensions", [d.key for d in self.dimensions]),
            ("llm_flags", [f.key for f in self.llm_flags]),
            ("keyword_penalties", [p.key for p in self.keyword_penalties]),
            ("sources", [s.type for s in self.sources]),
        ):
            dupes = {k for k in keys if keys.count(k) > 1}
            if dupes:
                raise ValueError(f"duplicate {name} keys: {sorted(dupes)}")
        return self

    @model_validator(mode="after")
    def _cv_source(self) -> UserProfile:
        if self.cv_path and self.cv_text:
            raise ValueError("supply cv_path or cv_text, not both")
        return self

    # -- helpers ------------------------------------------------------------

    @property
    def dimension_keys(self) -> list[str]:
        return [d.key for d in self.dimensions]

    @property
    def weights(self) -> dict[str, float]:
        return {d.key: d.weight for d in self.dimensions}

    @property
    def flag_adjustments(self) -> dict[str, float]:
        return {f.key: f.adjustment for f in self.llm_flags} | {
            p.key: p.adjustment for p in self.keyword_penalties
        }

    def enabled_sources(self) -> list[SourceConfig]:
        return [s for s in self.sources if s.enabled]

    def source(self, source_type: str) -> SourceConfig | None:
        return next((s for s in self.sources if s.type == source_type), None)

    def load_cv(self, base_dir: Path | None = None) -> str:
        """Resolve the CV text. Reads `cv_path` relative to `base_dir`.

        Raises rather than falling back to a stub: silently scoring against an
        empty CV would produce plausible-looking nonsense.
        """
        if self.cv_text:
            return self.cv_text
        if not self.cv_path:
            raise ValueError(
                f"profile {self.profile_id!r} has neither cv_text nor cv_path"
            )
        path = Path(self.cv_path)
        if base_dir and not path.is_absolute():
            path = base_dir / path
        if not path.exists():
            raise FileNotFoundError(
                f"profile {self.profile_id!r}: CV file not found at {path}. "
                f"CVs are intentionally not versioned - see ARCHITECTURE.md § 1."
            )
        return path.read_text(encoding="utf-8").strip()


def load_profile(path: str | Path, base_dir: Path | None = None) -> UserProfile:
    """Load and validate one profile JSON file."""
    path = Path(path)
    profile = UserProfile.model_validate(json.loads(path.read_text(encoding="utf-8")))
    if base_dir is None:
        base_dir = path.parent.parent
    profile.load_cv(base_dir)  # fail fast if the CV is missing
    return profile


# ---------------------------------------------------------------------------
# Profiles that come from Supabase rather than from disk
# ---------------------------------------------------------------------------

class EngineProfileError(ValueError):
    """What get-profile returned cannot become a UserProfile."""


def profile_from_engine(row: Mapping[str, Any], *, user_id: str) -> UserProfile:
    """Build a UserProfile from the row the get-profile route returns.

    `row` is Lovable's profile object: its own onboarding columns, plus
    `engine_scoring_profile` (the scoring document, same shape as a
    profiles/*.json file) and `cv_text` (the CV Lovable already extracted).

    This is the server-side counterpart of load_profile(). It touches no file,
    which is the point: on the deployment there is no profiles/ directory and
    no CV to read. It is a pure function of `row`, so it is tested without a
    network call.

    Nothing is ever invented. Every path out of here is either a valid profile
    the user actually configured, or an EngineProfileError naming what is
    missing.
    """
    if not isinstance(row, Mapping):
        raise EngineProfileError(
            f"get-profile returned {type(row).__name__}, not an object, "
            f"for user {user_id!r}."
        )

    raw = row.get("engine_scoring_profile")

    # The normal case for an account that has only been through onboarding.
    #
    # This raises rather than falling back to a default profile, and the reason
    # is persistence, not output quality. A default would score postings with
    # invented dimensions, those scores would be written to job_results, and
    # their URLs would join known_urls. The next run - the one with the real
    # profile - would then skip those postings as already seen. So the fallback
    # would spend Haiku money to permanently poison the deduplication set.
    # Failing here spends nothing and is undone by storing a profile.
    if raw is None or raw == "" or raw == {}:
        raise EngineProfileError(
            f"user {user_id!r} has no engine_scoring_profile. The engine "
            f"cannot score without dimensions, weights and thresholds, and "
            f"inventing them would write meaningless scores into job_results "
            f"and mark those postings as already seen, so the real profile "
            f"would never re-score them. Store a scoring profile for this "
            f"user first."
        )

    # A jsonb column arrives as an object, a text column as a string.
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise EngineProfileError(
                f"user {user_id!r}: engine_scoring_profile is a string, and "
                f"not valid JSON: {exc}"
            ) from exc
    if not isinstance(raw, Mapping):
        raise EngineProfileError(
            f"user {user_id!r}: engine_scoring_profile is "
            f"{type(raw).__name__}, expected an object."
        )

    document = dict(raw)

    # cv_path is meaningless here: it points into the repo, and the server has
    # no such file. Dropped rather than rejected, because the route supplies
    # the real text just below and the validator refuses to hold both.
    document.pop("cv_path", None)

    # cv_text goes to its own field, NOT to candidate_summary. They are two
    # different parts of the prompt: candidate_summary is the one-line headline
    # that introduces the CV (see prompts.build_eval_prompt), so overwriting it
    # with the CV would duplicate the CV and lose the headline.
    cv_text = row.get("cv_text")
    if not isinstance(cv_text, str) or not cv_text.strip():
        raise EngineProfileError(
            f"user {user_id!r}: get-profile returned no usable cv_text "
            f"({type(cv_text).__name__}). Scoring against an empty CV produces "
            f"plausible-looking nonsense, so this is an error rather than a "
            f"blank candidate section."
        )
    document["cv_text"] = cv_text.strip()

    try:
        return UserProfile.model_validate(document)
    except ValidationError as exc:
        raise EngineProfileError(
            f"user {user_id!r}: engine_scoring_profile is not a valid "
            f"profile. {exc}"
        ) from exc
