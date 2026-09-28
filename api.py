"""HTTP entry point for the job search engine.

    uvicorn api:app --host 0.0.0.0 --port $PORT

Runs are ASYNCHRONOUS. Measured durations: scoring 25 postings takes ~25s, but
a live LinkedIn fetch is ~25 minutes for its twelve Apify actors, and a full
run reaches ~35 minutes. Railway's proxy and every browser give up long before
that, so POST /run returns 202 with a run_id straight away and the work
continues in the background. GET /run/{run_id} polls.

Run state is written through the Store, not held in memory, so a poll still
works after a redeploy and two instances see the same thing.

    GET  /health            liveness, no auth
    GET  /                  what this service is
    POST /estimate          what a run would cost. Spends nothing.
    POST /run               start a run. SPENDS MONEY.
    GET  /run/{run_id}      status of a run
    POST /run/{id}/select   resume a run explicitly parked with
                            select_after_collect. SPENDS HAIKU. Not part of
                            the default flow: POST /run's own
                            scoring_percentage decides the volume up front,
                            alongside the cost confirmation, so a run never
                            has to wait on a choice that might not come. Kept
                            for a caller that still wants to see the real pool
                            before deciding.
    GET  /results           the scored offers (this is the deliverable)
    POST /suggest-keywords  broad keyword/job-title suggestions from a CV.
                            SPENDS SONNET, not Apify. Independent of
                            engine_scoring_profile - works before one exists
                            (onboarding) and again later to regenerate from
                            "Mon profil". Writes nothing.

Every route except /health and / requires the X-API-Key header. This endpoint
starts paid Haiku and Apify calls, so an unauthenticated one would let anyone
spend the operator's credits.
"""

from __future__ import annotations

import os
import threading
import time
import traceback
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from jobscout.collect import plan_run
from jobscout.connectors import Secrets
from jobscout.connectors.base import DumpStore
from jobscout.keyword_suggestions import ResponseError, suggest_keywords
from jobscout.pipeline import AWAITING_SELECTION, RunOptions, run as run_pipeline
from jobscout.profile import (
    BUDGET_TIERS, PAID_SOURCE_TYPES, EngineProfileError, UserProfile,
    load_profile, profile_from_engine,
)
from jobscout.store.base import RunRecord
from jobscout.store.json_store import JsonStore

from cost_guard import check_run_cost

REPO = Path(__file__).resolve().parent
PROFILES_DIR = REPO / "profiles"
# Where raw Apify payloads are kept. Configurable because the resume flow
# below replays them: on a host with an ephemeral filesystem this should point
# at a mounted disk, or a run held for selection loses its payload on restart
# and can only refuse to resume.
DUMPS_DIR = Path(os.environ.get("DUMPS_DIR", str(REPO / "dumps")))

# Render spins a free service down after 15 minutes without INBOUND traffic,
# and a background task is not inbound traffic. A run takes 25-35 minutes, so
# a user who closes the tab stops the frontend's polling and the service can
# be stopped mid-run - with the Apify money already spent.
#
# RENDER_EXTERNAL_URL is set by Render itself and is empty everywhere else,
# which is what disables all of this locally and in tests. The ping goes to the
# PUBLIC url on purpose: a request to localhost never reaches Render's proxy,
# which is what measures traffic, so it would not count.
PUBLIC_URL = os.environ.get("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
KEEPALIVE_INTERVAL = float(os.environ.get("KEEPALIVE_INTERVAL_SECONDS", "600"))
KEEPALIVE_MAX = float(os.environ.get("KEEPALIVE_MAX_SECONDS", "3600"))

# How long the service is held awake for a run parked at AWAITING_SELECTION,
# waiting for someone to choose a volume. Nothing is executing then, but the
# saved Apify payload lives on a filesystem that a spin-down destroys, so
# sleeping here throws away collection that has already been paid for.
KEEPALIVE_AWAITING_MAX = float(
    os.environ.get("KEEPALIVE_AWAITING_MAX_SECONDS", "1800"))

# run_id -> the event that releases its hold. Only for runs at the pause.
_selection_holds: dict[str, threading.Event] = {}

# If KEEPALIVE_AWAITING_MAX passes with nobody having chosen a volume, this
# fraction of the collected pool is scored automatically rather than stranding
# a collection that has already been paid for (see
# _auto_select_on_timeout). 0.70 matches the "recommended" choice already
# offered on Lovable's own selector, so the automatic fallback lands on the
# same volume most people would have picked anyway.
AUTO_SELECT_VOLUME_FRACTION = float(
    os.environ.get("AUTO_SELECT_VOLUME_FRACTION", "0.70"))

API_KEY = os.environ.get("JOBSCOUT_API_KEY", "").strip()
STORE_BACKEND = os.environ.get("STORE_BACKEND", "json").strip().lower()
STORE_DIR = os.environ.get("STORE_DIR", str(REPO / "store_data")).strip()

# Comma-separated list of origins allowed to call this API from a browser.
# Lovable runs on its own domain, so it has to be listed explicitly.
ALLOWED_ORIGINS = [
    o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()
]

app = FastAPI(
    title="Job Scout engine",
    version="1.0.0",
    description="Scores job postings against a user profile. See ARCHITECTURE.md.",
)

app.add_middleware(
    CORSMiddleware,
    # No wildcard fallback: an unset ALLOWED_ORIGINS blocks browser calls
    # rather than opening the API to every site.
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["X-API-Key", "Content-Type"],
)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _log(message: str) -> None:
    """Stdout, unbuffered, which is what Render collects as service logs."""
    print(message, flush=True)


def require_api_key(x_api_key: str = Header(default="")) -> None:
    """Shared-secret check on every route that can spend money or read results.

    When JOBSCOUT_API_KEY is unset the service refuses these routes outright,
    rather than serving them openly: a deployment that forgot the variable
    would otherwise hand anyone the ability to run up an Anthropic bill.
    """
    if not API_KEY:
        raise HTTPException(
            status_code=503,
            detail=("JOBSCOUT_API_KEY is not configured on the server. "
                    "Protected routes are disabled until it is set."),
        )
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")


Protected = Depends(require_api_key)


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

def get_store():
    """The Store implementation this deployment uses.

    JsonStore writes to disk, which on Railway is ephemeral - fine for a first
    deploy, not for real use. Set STORE_BACKEND=lovable to persist through
    Lovable's engine routes instead; that needs ENGINE_API_KEY.

    The Lovable backend now serves runs and results too (get-run,
    get-job-results, get-known-urls). save_application is the one Store method
    with no route behind it yet, and it raises rather than dropping the write.
    """
    if STORE_BACKEND in ("supabase", "lovable"):
        from jobscout.store.supabase_store import LovableEngineStore
        return LovableEngineStore.from_env()
    return JsonStore(STORE_DIR)


def dumps_dir_for(user_id: str) -> Path:
    """Where this user's raw Apify payloads live.

    Per user, because the dump filename is keyed on profile_id alone and two
    accounts can easily both store a profile called "gabriel". Sharing one
    directory would let one user's run overwrite another's payload, and the
    selection flow below replays that payload rather than re-paying for it -
    so a collision would silently score the wrong person's postings.
    """
    safe = user_id.replace("/", "_").replace("\\", "_").lstrip(".") or "anonymous"
    return DUMPS_DIR / safe


def profile_from_file(profile_id: str) -> UserProfile:
    """Load profiles/<id>.json, with a 404 rather than a stack trace."""
    if not profile_id or "/" in profile_id or "\\" in profile_id or profile_id.startswith("."):
        raise HTTPException(status_code=400, detail=f"invalid profile_id: {profile_id!r}")
    path = PROFILES_DIR / f"{profile_id}.json"
    if not path.exists():
        available = sorted(p.stem for p in PROFILES_DIR.glob("*.json"))
        raise HTTPException(
            status_code=404,
            detail=f"no profile {profile_id!r}. Available: {available}",
        )
    try:
        return load_profile(path, base_dir=REPO)
    except Exception as exc:
        raise HTTPException(
            status_code=422, detail=f"profile {profile_id!r} is invalid: {exc}"
        ) from exc


def resolve_profile(profile_id: str, user_id: str | None) -> tuple[UserProfile, str]:
    """The profile to run and the user it belongs to.

    The two backends are deliberately not merged. STORE_BACKEND=json reads
    profiles/*.json exactly as before, so local runs and the existing tests are
    untouched. STORE_BACKEND=lovable reads the profile out of Supabase through
    get-profile, keyed by user: the deployment has no profiles directory worth
    reading and no CV file at all, so profile_id means nothing there.
    """
    if STORE_BACKEND in ("supabase", "lovable"):
        if not user_id:
            raise HTTPException(
                status_code=400,
                detail=("user_id is required when STORE_BACKEND=lovable: the "
                        "profile is read from Supabase for that user, not from "
                        "a local file. profile_id is ignored in this mode."),
            )
        try:
            row = get_store().get_profile(user_id)
        except HTTPException:
            raise
        except Exception as exc:
            # The route is unreachable or answered badly. That is the engine's
            # dependency failing, not the caller's request being wrong.
            raise HTTPException(
                status_code=502,
                detail=f"get-profile failed for {user_id!r}: "
                       f"{type(exc).__name__}: {exc}",
            ) from exc
        try:
            return profile_from_engine(row, user_id=user_id), user_id
        except EngineProfileError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    profile = profile_from_file(profile_id)
    return profile, (user_id or profile.profile_id)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class RunRequest(BaseModel):
    profile_id: str = Field(
        default="",
        description=("Which profiles/*.json to run, e.g. 'gabriel'. Used only "
                     "when STORE_BACKEND=json; ignored otherwise."),
    )
    user_id: str | None = Field(
        default=None,
        description=("Row owner, a Supabase auth uuid. Required when "
                     "STORE_BACKEND=lovable, since the profile is read for "
                     "that user. Defaults to profile_id locally."),
    )
    confirmed_fingerprint: str | None = Field(
        default=None,
        description=("Set to the fingerprint from a NEEDS_CONFIRMATION response "
                     "to authorise that exact run."),
    )
    scoring_percentage: int | None = Field(
        default=70,
        ge=0, le=100,
        description=("How much of the real, post-dedup pool to score, decided "
                     "up front alongside the collection cost confirmation - "
                     "not after a pause that might never get answered. 70 "
                     "(the recommended choice on Lovable's own selector) if "
                     "not given. Applied once the real pool is known, so "
                     "resolves to an absolute count internally: 70 against a "
                     "434-posting pool scores round(434 * 0.70) = 304."),
    )
    max_jobs_to_score: int | None = Field(
        default=None,
        description=("A hard ceiling on top of scoring_percentage, not a "
                     "replacement for it - dev safety net so a typo in a "
                     "keyword list cannot spend money on ~450 postings by "
                     "accident. Whichever of the two caps the pool harder "
                     "wins. None (the default) means only scoring_percentage "
                     "applies."),
    )
    generate_text: bool = Field(
        default=True, description="Also write cover letters / emails (Sonnet)."
    )
    reuse_dumps: bool = Field(
        default=False,
        description=("Replay saved Apify payloads instead of calling Apify. "
                     "Costs nothing and is much faster."),
    )
    select_after_collect: bool = Field(
        default=False,
        description=("Stop after collection with the real pool size and score "
                     "nothing yet. The run reaches AWAITING_SELECTION; resume "
                     "it with POST /run/{run_id}/select."),
    )


class SelectionRequest(BaseModel):
    """How many of the collected postings to actually score."""

    user_id: str = Field(description="Owner of the run")
    max_jobs_to_score: int | None = Field(
        default=None,
        description=("Absolute number, not a percentage: the pool is "
                     "recollected on resume and free feeds move, so a "
                     "percentage would resolve to a different count than the "
                     "one displayed. None scores the whole pool."),
    )
    generate_text: bool = Field(
        default=True, description="Also write cover letters / emails (Sonnet)."
    )


class SuggestKeywordsRequest(BaseModel):
    user_id: str | None = Field(
        default=None,
        description=("Row owner, a Supabase auth uuid. Required when "
                     "STORE_BACKEND=lovable and cv_text is not given "
                     "directly, since cv_text is then read for this user."),
    )
    cv_text: str | None = Field(
        default=None,
        description=("Overrides whatever cv_text is stored for user_id - use "
                     "this to suggest from CV text just pasted or "
                     "re-uploaded but not yet saved."),
    )
    language: str | None = Field(
        default=None,
        description=("Language for the suggestions themselves (\"en\", "
                     "\"es\", \"fr\", \"ca\", or any language name). None "
                     "lets the model infer it from the CV's own language."),
    )


class SuggestKeywordsResponse(BaseModel):
    suggestions: list[str]


class RunAccepted(BaseModel):
    run_id: str
    status: Literal["RUNNING"] = "RUNNING"
    profile_id: str
    poll: str


class RunStatus(BaseModel):
    run_id: str
    status: str
    profile_id: str = ""
    started_at: str = ""
    finished_at: str = ""
    cost_decision: str = ""
    cost_estimate: dict[str, Any] = Field(default_factory=dict)
    cost_fingerprint: str = ""
    counts: dict[str, int] = Field(default_factory=dict)


def _apply_reuse_dumps(profile: UserProfile) -> UserProfile:
    return profile.model_copy(update={"sources": [
        s.model_copy(update={"reuse_dump": True})
        if s.type in ("infojobs_apify", "linkedin_apify") else s
        for s in profile.sources
    ]})


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
def health() -> dict:
    """Liveness for Railway. Deliberately unauthenticated and side-effect free."""
    secrets = Secrets.from_env()
    return {
        "status": "ok",
        "time": datetime.now().isoformat(),
        "store_backend": STORE_BACKEND,
        "profiles_available": sorted(p.stem for p in PROFILES_DIR.glob("*.json"))
        if PROFILES_DIR.exists() else [],
        # Whether each credential is present. Never the values.
        "config": {
            "api_key_set": bool(API_KEY),
            "anthropic_key_set": bool(secrets.anthropic_api_key),
            "apify_token_set": bool(secrets.apify_token),
            "cors_origins": len(ALLOWED_ORIGINS),
        },
    }


@app.get("/")
def root() -> dict:
    return {
        "service": "Job Scout engine",
        "runs_are": "asynchronous — POST /run returns a run_id, then poll GET /run/{run_id}",
        "routes": ["/health", "/estimate", "/run", "/run/{run_id}",
                   "/run/{run_id}/select", "/results", "/suggest-keywords"],
        "auth": "X-API-Key header on every route except /health and /",
    }


@app.post("/estimate", dependencies=[Protected])
def estimate(request: RunRequest) -> dict:
    """What this run would cost. Makes no network call and spends nothing.

    Apify cost only. The Haiku and Sonnet spend depends on how many postings
    survive filtering, which is not known until they are fetched.
    """
    profile, user_id = resolve_profile(request.profile_id, request.user_id)
    if request.reuse_dumps:
        profile = _apply_reuse_dumps(profile)

    plans, _ = plan_run(profile, dumps_dir_for(user_id))
    result = check_run_cost(plans, profile.cost_policy,
                            confirmed_fingerprint=request.confirmed_fingerprint)
    payload = result.to_dict()
    payload["note"] = (
        "Apify only. Scoring adds roughly $0.005 per posting in Haiku calls, "
        "for whatever share of the real post-dedup pool scoring_percentage "
        "resolves to on POST /run - not knowable here, since this estimate "
        "runs before collection and only sees the raw worst-case count."
    )

    # Which sources each budget tier would activate for THIS profile, so a
    # frontend can grey out a tier that would run the exact same sources as a
    # cheaper one (e.g. "max" adding nothing over "standard" until a second
    # paid, relevant-to-them source exists).
    by_tier = {t: sorted(s.type for s in profile.active_sources(t)) for t in BUDGET_TIERS}
    payload["budget_tiers"] = {
        "current": profile.budget_tier,
        "sources_by_tier": by_tier,
        "redundant_tiers": [
            t for i, t in enumerate(BUDGET_TIERS)
            if i > 0 and by_tier[t] == by_tier[BUDGET_TIERS[i - 1]]
        ],
    }
    return payload


@app.post("/run", status_code=202, dependencies=[Protected])
def start_run(request: RunRequest, background: BackgroundTasks) -> RunAccepted:
    """Start a run. SPENDS MONEY once the cost guard approves.

    Returns immediately with a run_id; poll GET /run/{run_id}. A run held for
    cost confirmation finishes almost at once with NEEDS_CONFIRMATION and the
    fingerprint to send back.
    """
    profile, user_id = resolve_profile(request.profile_id, request.user_id)
    if request.reuse_dumps:
        profile = _apply_reuse_dumps(profile)

    secrets = Secrets.from_env()
    if not secrets.anthropic_api_key:
        raise HTTPException(
            status_code=503,
            detail="ANTHROPIC_API_KEY is not configured on the server.",
        )

    store = get_store()
    run_id = uuid.uuid4().hex[:12]

    # Recorded before the work starts, so the first poll has something to read
    # instead of a 404.
    store.save_run(RunRecord(
        run_id=run_id, user_id=user_id, profile_id=profile.profile_id,
        started_at=datetime.now().isoformat(), status="RUNNING",
    ))

    opts = RunOptions(
        confirmed_fingerprint=request.confirmed_fingerprint,
        scoring_percentage=request.scoring_percentage,
        max_jobs_to_score=request.max_jobs_to_score,
        generate_text=request.generate_text,
        select_after_collect=request.select_after_collect,
        dumps_dir=str(dumps_dir_for(user_id)),
    )
    background.add_task(_execute, profile, store, secrets, opts, user_id, run_id)
    return RunAccepted(run_id=run_id, profile_id=profile.profile_id,
                       poll=f"/run/{run_id}")


class _ResumeRefused(Exception):
    """A held run cannot be resumed. Carries the HTTP status a real caller
    needs; the keepalive's own automatic fallback just logs it and gives up,
    since nobody is waiting on a response."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _resume_held_run(
    run_id: str, user_id: str, *, max_jobs_to_score: int | None,
    generate_text: bool, auto: bool,
) -> tuple[UserProfile, Secrets, RunOptions, str]:
    """Validate a run parked at AWAITING_SELECTION and prepare it to be
    scored. Does not itself call _execute: the two callers run it differently
    (backgrounded after an HTTP 202, or inline on the keepalive's own thread).

    Shared by POST /run/{id}/select (a real choice) and
    _auto_select_on_timeout (nobody answered before the keepalive gave up).
    `auto` marks the run's counts so the frontend can tell an automatic
    default from a real choice - see the module docstring change of
    2026-09-27.

    Raises _ResumeRefused naming exactly why, in every case where resuming
    would be wrong: the run does not exist, is not awaiting a choice, the
    server cannot score right now, or the paid Apify payload is gone (this
    check is what stands between a mistake and paying for the same collection
    twice - do not weaken it to make the keepalive path more permissive).
    """
    store = get_store()
    record = store.get_run(user_id, run_id)
    if record is None:
        raise _ResumeRefused(404, f"no run {run_id!r} for {user_id!r}")
    if record.status != AWAITING_SELECTION:
        raise _ResumeRefused(409, (
            f"run {run_id!r} is {record.status}, not {AWAITING_SELECTION}. "
            f"Only a run waiting for a volume choice can be resumed this "
            f"way."))

    profile, user_id = resolve_profile(record.profile_id, user_id)
    secrets = Secrets.from_env()
    if not secrets.anthropic_api_key:
        raise _ResumeRefused(
            503, "ANTHROPIC_API_KEY is not configured on the server.")

    # Replaying is what makes this free. Check the payload is actually there
    # before promising anything: without it the connector would fall back to
    # a live fetch and bill the collection twice.
    dumps_dir = dumps_dir_for(user_id)
    profile = _apply_reuse_dumps(profile)
    dumps = DumpStore(dumps_dir)
    missing = [
        s.type for s in profile.active_sources()
        if s.type in PAID_SOURCE_TYPES
        and not dumps.path_for(s.type, profile.profile_id).exists()
    ]
    if missing:
        raise _ResumeRefused(409, (
            f"the saved payload for {', '.join(missing)} is gone, so "
            f"resuming would re-scrape and charge for collection again. "
            f"Start a new run instead."))

    # The run's own keepalive takes over from here.
    _release_selection_hold(run_id)

    counts = dict(record.counts)
    if auto:
        counts["volume_auto_selected"] = 1

    store.save_run(RunRecord(
        run_id=run_id, user_id=user_id, profile_id=profile.profile_id,
        started_at=record.started_at or datetime.now().isoformat(),
        status="RUNNING", counts=counts,
    ))
    opts = RunOptions(
        max_jobs_to_score=max_jobs_to_score,
        generate_text=generate_text,
        dumps_dir=str(dumps_dir),
    )
    return profile, secrets, opts, user_id


@app.post("/run/{run_id}/select", status_code=202, dependencies=[Protected])
def select_volume(
    run_id: str, request: SelectionRequest, background: BackgroundTasks
) -> RunAccepted:
    """Resume a run held at AWAITING_SELECTION, scoring the chosen number.

    Spends Haiku, and only Haiku: collection is replayed from the Apify payload
    saved by the first call, so Apify is not paid a second time. If that saved
    payload is gone - the deployment restarted between the two calls, and its
    filesystem is ephemeral - this refuses with 409 rather than silently
    re-scraping and re-charging.
    """
    try:
        profile, secrets, opts, user_id = _resume_held_run(
            run_id, request.user_id,
            max_jobs_to_score=request.max_jobs_to_score,
            generate_text=request.generate_text, auto=False)
    except _ResumeRefused as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    background.add_task(_execute, profile, get_store(), secrets, opts,
                        user_id, run_id)
    return RunAccepted(run_id=run_id, profile_id=profile.profile_id,
                       poll=f"/run/{run_id}")


def _keepalive(stop: threading.Event, run_id: str,
               max_seconds: float | None = None) -> str:
    """Keep the service awake for as long as one run is executing.

    Render does not document whether a service's request to its own public URL
    counts as the inbound traffic that prevents a spin-down. MEASURED
    2026-09-17 on the free instance: this exact function, pinging for 25
    minutes with nothing else touching the service, left the process alive for
    36.7 minutes with two pings at 200 and no restart - where 15 minutes of
    quiet would have stopped it. It counts. The ping goes to the public URL
    for that reason; localhost never reaches the proxy that measures traffic.

    Every ping is still logged with its status code, so a future change in
    Render's behaviour shows up in the service logs rather than as runs that
    mysteriously stop.

    Two bounds, both deliberate. The interval stays under the 15-minute
    threshold with room to spare. The deadline stops the pinging even if the
    pipeline never returns, so a hung run cannot hold the service awake
    indefinitely - free instance hours are capped at 750 a month for the whole
    workspace, and exhausting them suspends every free service until the next.

    Returns why it stopped - "disabled" (no PUBLIC_URL, nothing ever ran),
    "released" (`stop` was set before the deadline), or "deadline" (the bound
    was reached first). Only the awaiting-selection hold inspects this, to
    tell "a real choice arrived" from "nobody ever answered" - see
    _hold_awake_for_selection.
    """
    if not PUBLIC_URL:
        return "disabled"
    limit = KEEPALIVE_MAX if max_seconds is None else max_seconds
    deadline = time.monotonic() + limit
    while not stop.wait(KEEPALIVE_INTERVAL):
        if time.monotonic() > deadline:
            _log(f"[keepalive] {run_id}: {limit:.0f}s deadline reached, "
                 f"stopping. The run is still going but will no longer hold "
                 f"the service awake.")
            return "deadline"
        try:
            response = httpx.get(f"{PUBLIC_URL}/health", timeout=15.0)
            _log(f"[keepalive] {run_id}: GET {PUBLIC_URL}/health -> "
                 f"{response.status_code}")
        except Exception as exc:
            _log(f"[keepalive] {run_id}: GET {PUBLIC_URL}/health failed: "
                 f"{type(exc).__name__}: {exc}")
    return "released"


def _auto_select_on_timeout(run_id: str, user_id: str) -> None:
    """Nobody chose a volume before the awaiting-selection hold gave up.

    CONFIRMED on a real run, 2026-09-27: [pipeline] awaiting selection: 434
    postings collected, none scored, followed 30 minutes later by 1800s
    deadline reached, stopping - and the run just sat there in
    AWAITING_SELECTION forever, the ~$2.40 of Apify collection never scored.
    Losing money that was already spent, for want of one more decision, is
    worse than guessing - so this scores AUTO_SELECT_VOLUME_FRACTION of the
    pool rather than leave it stranded.

    Runs inline on the keepalive's own daemon thread, not backgrounded: that
    thread exists only to hold the service awake and, now, to make this one
    decision if nobody else does - blocking it for the length of the run is
    exactly what it is for.
    """
    store = get_store()
    record = store.get_run(user_id, run_id)
    available = record.counts.get("available_to_score", 0) if record else 0
    default_n = round(available * AUTO_SELECT_VOLUME_FRACTION)

    try:
        profile, secrets, opts, user_id = _resume_held_run(
            run_id, user_id, max_jobs_to_score=default_n,
            generate_text=True, auto=True)
    except _ResumeRefused as exc:
        _log(f"[auto-select] {run_id}: cannot auto-resume ({exc.status_code}): "
             f"{exc.detail}")
        return

    _log(f"[auto-select] {run_id}: nobody chose a volume in "
         f"{KEEPALIVE_AWAITING_MAX / 60:.0f} min; scoring {default_n}/"
         f"{available} ({AUTO_SELECT_VOLUME_FRACTION:.0%}) automatically")
    _execute(profile, store, secrets, opts, user_id, run_id)

    # pipeline.run()'s own final save (jobscout/pipeline.py _finish) replaces
    # counts wholesale with its freshly-recomputed dict - it has no notion of
    # "auto" and would silently overwrite the marker set above. Re-stamp it on
    # whatever _execute just wrote, so a run that reaches OK/FAILED still
    # says how its volume was chosen.
    final = store.get_run(user_id, run_id)
    if final is not None:
        store.save_run(final.model_copy(
            update={"counts": {**final.counts, "volume_auto_selected": 1}}))


def _hold_awake_for_selection(run_id: str, user_id: str) -> None:
    """Keep the service alive while a run waits for its volume to be chosen.

    This was originally left out, on the reasoning that the screen showing the
    selector would be polling anyway and that polling was already the
    keepalive. MEASURED 2026-09-21: the screen never appeared, nothing polled,
    and the service was stopped fifteen minutes after the pause - taking with
    it a collection that had already cost $2.40.

    The lesson is that the keepalive cannot depend on a frontend behaving as
    expected. It is bounded all the same: after KEEPALIVE_AWAITING_MAX nobody
    is coming, and _auto_select_on_timeout now covers the payload that would
    otherwise be lost, rather than just the 409 that used to be the only
    acknowledgment of it.
    """
    previous = _selection_holds.pop(run_id, None)
    if previous is not None:
        previous.set()

    release = threading.Event()
    _selection_holds[run_id] = release

    def hold() -> None:
        try:
            outcome = _keepalive(release, f"{run_id} (awaiting selection)",
                                 max_seconds=KEEPALIVE_AWAITING_MAX)
            # Re-check that this hold is still the live one: a real /select
            # call releases it (and _keepalive would then have returned
            # "released", not "deadline"), but a wafer-thin window remains
            # between the deadline firing and this line. If a concurrent
            # caller already popped this exact hold, back off rather than
            # risk scoring the same collection twice.
            if outcome == "deadline" and _selection_holds.get(run_id) is release:
                _auto_select_on_timeout(run_id, user_id)
        finally:
            # Never leave an entry behind for a hold that has ended, or the
            # registry grows for the life of the process.
            if _selection_holds.get(run_id) is release:
                del _selection_holds[run_id]

    threading.Thread(target=hold, name=f"keepalive-awaiting-{run_id}",
                     daemon=True).start()
    _log(f"[keepalive] {run_id}: held awake for up to "
         f"{KEEPALIVE_AWAITING_MAX / 60:.0f} min awaiting a volume choice")


def _release_selection_hold(run_id: str) -> None:
    """Stop holding: the choice arrived, or the run moved on."""
    release = _selection_holds.pop(run_id, None)
    if release is not None:
        release.set()


def _execute(profile, store, secrets, opts: RunOptions,
             user_id: str, run_id: str) -> None:
    """The background job. Any failure is recorded, never swallowed.

    The keepalive is started and stopped here rather than from a registry of
    active runs: its lifetime IS the lifetime of this call, so there is no
    state to leak and no way for a ping to outlive the work. An exception, an
    early return, a cost guard refusal - everything reaches the finally. If the
    process is killed outright, the daemon thread dies with it.
    """
    stop = threading.Event()
    threading.Thread(target=_keepalive, args=(stop, run_id),
                     name=f"keepalive-{run_id}", daemon=True).start()
    report = None
    try:
        report = run_pipeline(
            profile, store, secrets, opts,
            user_id=user_id, repo_dir=REPO, run_id=run_id,
        )
    except Exception as exc:
        traceback.print_exc()
        try:
            store.save_run(RunRecord(
                run_id=run_id, user_id=user_id, profile_id=profile.profile_id,
                finished_at=datetime.now().isoformat(), status="FAILED",
                counts={}, cost_decision=f"{type(exc).__name__}: {exc}"[:200],
            ))
        except Exception:
            traceback.print_exc()
    finally:
        stop.set()

    # The run is over, but a run parked at the pause is not finished with the
    # service: its payload has to survive until someone picks a volume.
    if report is not None and report.status == AWAITING_SELECTION:
        _hold_awake_for_selection(run_id, user_id)


@app.get("/run/{run_id}", dependencies=[Protected])
def run_status(
    run_id: str,
    user_id: str = Query(..., description="Owner of the run"),
) -> RunStatus:
    """Poll a run.

    status is RUNNING, then one of OK / NEEDS_CONFIRMATION /
    AWAITING_SELECTION / REJECTED_OVER_HARD_CAP / FAILED.

    On NEEDS_CONFIRMATION, resend POST /run with cost_fingerprint as
    confirmed_fingerprint AND scoring_percentage - both are decided together,
    before collection. AWAITING_SELECTION should not occur for a run that set
    scoring_percentage; it only appears for one that explicitly asked to
    pause with select_after_collect, in which case counts.available_to_score
    holds the real pool and POST /run/{run_id}/select takes the number to
    score.
    """
    record = get_store().get_run(user_id, run_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"no run {run_id!r} for {user_id!r}")
    return RunStatus(**record.model_dump())


@app.get("/results", dependencies=[Protected])
def results(
    user_id: str = Query(..., description="Whose results to read"),
    verdict: list[str] | None = Query(
        default=None, description="Filter: YES, MAYBE, NO. Repeatable."),
    limit: int = Query(default=100, ge=1, le=1000),
) -> dict:
    """The scored offers. This is the deliverable the frontend renders."""
    rows = get_store().get_results(user_id, verdicts=verdict)
    rows.sort(key=lambda r: -r.score)
    return {
        "user_id": user_id,
        "total": len(rows),
        "returned": min(len(rows), limit),
        "results": [r.model_dump() for r in rows[:limit]],
    }


@app.post("/suggest-keywords", dependencies=[Protected])
def suggest_keywords_route(request: SuggestKeywordsRequest) -> SuggestKeywordsResponse:
    """Propose 3-5 broad keyword/job-title suggestions from a CV.

    Deliberately independent of engine_scoring_profile: this must work
    before any scoring profile exists (onboarding's "explore broadly, let
    Claude propose" branch), and the exact same call regenerates suggestions
    later from "Mon profil", picking up whatever cv_text is stored at that
    moment - no versioning, no separate route. Writes nothing: the caller
    decides what to keep, drop or add before ever saving a profile.

    Billed: one Sonnet call. Not gated by cost_guard.py, which prices Apify
    specifically - this is a single small call, not a per-posting or
    per-search-URL cost.
    """
    cv_text = request.cv_text
    if not cv_text:
        if STORE_BACKEND not in ("supabase", "lovable"):
            raise HTTPException(
                status_code=422,
                detail=("cv_text must be given directly: STORE_BACKEND=json "
                        "has no get-profile route to read it from."))
        if not request.user_id:
            raise HTTPException(
                status_code=422,
                detail="user_id is required when cv_text is not given directly.")
        row = get_store().get_profile(request.user_id)
        cv_text = row.get("cv_text") if isinstance(row, dict) else None

    if not isinstance(cv_text, str) or not cv_text.strip():
        raise HTTPException(
            status_code=422,
            detail=(f"no usable cv_text for {request.user_id!r}. Generating "
                    f"suggestions from an empty CV would be meaningless, so "
                    f"this refuses rather than guessing."))

    secrets = Secrets.from_env()
    if not secrets.anthropic_api_key:
        raise HTTPException(
            status_code=503,
            detail="ANTHROPIC_API_KEY is not configured on the server.")

    import anthropic
    client = anthropic.Anthropic(api_key=secrets.anthropic_api_key)
    try:
        suggestions = suggest_keywords(
            client, cv_text.strip(), language=request.language)
    except ResponseError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"could not generate keyword suggestions: {exc}") from exc

    return SuggestKeywordsResponse(suggestions=suggestions)
